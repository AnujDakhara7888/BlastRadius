"""End-to-end impact, selection, and scoring pipeline."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path

from blastradius import __version__
from blastradius.coverage.profile import load_profile, validate_profile
from blastradius.errors import BlastRadiusError
from blastradius.models import (
    RISK_MODEL_VERSION,
    SCHEMA_VERSION,
    AnalysisReport,
    ChangedSymbol,
    DependencyEdge,
    EdgeKind,
    FallbackState,
    ImpactEvidence,
    RevisionIdentity,
    RiskScore,
    TestSelection,
)
from blastradius.runtime.profile import load_runtime_profile
from blastradius.static.analyzer import StaticIndex, build_index
from blastradius.static.changes import map_changes
from blastradius.vcs.git import (
    changed_paths,
    diff_files,
    list_python_files,
    read_file,
    resolve_revision,
)


def _sources(repo: Path, revision: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in list_python_files(repo, revision):
        source = read_file(repo, revision, path)
        if source is not None:
            result[path] = source
    return result


def _reverse_impact(
    changes: list[ChangedSymbol], indexes: list[StaticIndex]
) -> list[ImpactEvidence]:
    incoming: dict[str, list[tuple[str, float, str]]] = defaultdict(list)
    for index in indexes:
        for edge in index.edges:
            incoming[edge.callee].append((edge.caller, edge.confidence, edge.provenance))

    findings: dict[tuple[str, str], ImpactEvidence] = {}
    for change in changes:
        root_symbol = change.head or change.base
        if root_symbol is None:
            continue
        root_identities = {
            (symbol.id.path, symbol.id.qualified_name)
            for symbol in (change.base, change.head)
            if symbol is not None
        }
        possible_roots = [
            symbol.id.key
            for index in indexes
            for symbol in index.symbols.values()
            if (symbol.id.path, symbol.id.qualified_name) in root_identities
        ]
        for graph_root in sorted(set(possible_roots)):
            queue: deque[tuple[str, tuple[str, ...], float, tuple[str, ...]]] = deque(
                [(graph_root, (graph_root,), 1.0, ())]
            )
            visited = {graph_root}
            while queue:
                current, path, confidence, sources = queue.popleft()
                key = (root_symbol.id.key, current)
                existing = findings.get(key)
                finding = ImpactEvidence(
                    root_symbol.id.key,
                    current,
                    path,
                    "incoming_dependents",
                    round(confidence, 4),
                    tuple(sorted(set(sources or ("changed_root",)))),
                )
                if existing is None or finding.confidence > existing.confidence:
                    findings[key] = finding
                for caller, edge_confidence, source in sorted(incoming.get(current, [])):
                    if caller in visited:
                        continue
                    visited.add(caller)
                    queue.append(
                        (caller, (*path, caller), confidence * edge_confidence, (*sources, source))
                    )
    return sorted(findings.values(), key=lambda item: (item.changed_root, item.affected_symbol))


def _runtime_index(path: Path, base: RevisionIdentity, static: StaticIndex) -> StaticIndex:
    raw = load_runtime_profile(path)
    try:
        revision = raw["revision"]
        if raw["schema_version"] != SCHEMA_VERSION:
            raise BlastRadiusError("runtime profile schema is not supported")
        if revision["repository"] != base.repository or revision["sha"] != base.sha:
            raise BlastRadiusError(
                "runtime profile identity does not match the resolved base revision; retrace it"
            )
        if raw["source_type"] != "representative_workload":
            raise BlastRadiusError("runtime profile source type is not supported")
    except (KeyError, TypeError) as exc:
        raise BlastRadiusError(f"invalid runtime profile {path}: {exc}") from exc

    aliases = {
        f"{symbol.id.path}:{symbol.id.qualified_name}": symbol.id.key
        for symbol in static.symbols.values()
    }
    runtime = StaticIndex()
    for row in raw.get("edges", []):
        caller = aliases.get(row.get("caller", ""))
        callee = aliases.get(row.get("callee", ""))
        if caller and callee:
            runtime.edges.append(
                DependencyEdge(
                    caller,
                    callee,
                    EdgeKind.RUNTIME_CALL,
                    "representative_workload",
                    1.0,
                    int(row.get("observed_count", 0)),
                )
            )
    return runtime


def _select_tests(
    mode: str,
    changes: list[ChangedSymbol],
    impact: list[ImpactEvidence],
    base_index: StaticIndex,
    coverage_path: Path | None,
    base_identity: RevisionIdentity,
    modified_paths: set[str],
    changed_test_ids: set[str],
    unsupported_boundary: bool,
) -> tuple[TestSelection, list[str]]:
    warnings: list[str] = []
    if coverage_path is None:
        warnings.append(
            "Coverage evidence is missing; conservative policy requires a full-suite fallback."
        )
        return (
            TestSelection(
                mode, [], [], {}, FallbackState.FULL_SUITE, ["all_impact_obligations"], []
            ),
            warnings,
        )
    profile = load_profile(coverage_path)
    validate_profile(profile, base_identity)
    available = sorted(profile.test_lines)
    affected_keys = {item.affected_symbol for item in impact}
    obligations: dict[str, tuple[str, set[int]]] = {}
    for key in affected_keys:
        symbol = base_index.symbols.get(key)
        is_test_source = symbol and (
            symbol.id.path.startswith("tests/") or Path(symbol.id.path).name.startswith("test_")
        )
        if symbol and not is_test_source:
            obligations[f"symbol:{symbol.id.key}"] = (
                symbol.id.path,
                set(range(symbol.start_line, symbol.end_line + 1)),
            )
    for change in changes:
        if change.base:
            changed_lines = set(change.base_lines)
            structural_only = changed_lines and all(
                line < change.base.body_start_line for line in changed_lines
            )
            obligation_name = (
                f"changed-structure:{change.base.id.key}"
                if structural_only
                else f"changed-region:{change.base.id.key}"
            )
            obligations[obligation_name] = (
                change.base.id.path,
                set(range(change.base.start_line, change.base.end_line + 1))
                if structural_only
                else changed_lines,
            )

    reasons: dict[str, list[str]] = defaultdict(list)
    selected: set[str] = set()
    covered_obligations: set[str] = set()
    for test_id, files in sorted(profile.test_lines.items()):
        for path, lines in sorted(files.items()):
            for obligation, (obligation_path, obligation_lines) in obligations.items():
                if path != obligation_path:
                    continue
                overlap = obligation_lines.intersection(lines)
                if overlap:
                    selected.add(test_id)
                    reasons[test_id].append(
                        f"covers {obligation} lines {','.join(map(str, sorted(overlap)[:8]))}"
                    )
                    covered_obligations.add(obligation)
        test_path = test_id.split("::", 1)[0]
        if test_path in modified_paths:
            selected.add(test_id)
            reasons[test_id].append("test file changed in this revision range")

    for test_id in sorted(changed_test_ids):
        selected.add(test_id)
        reasons[test_id].append("new or changed test discovered at the head revision")

    uncovered = sorted(set(obligations) - covered_obligations)
    fallback = FallbackState.NONE
    if unsupported_boundary and mode == "conservative":
        fallback = FallbackState.FULL_SUITE
        selected.update(available)
        warnings.append(
            "A dependency, pytest configuration, or shared-fixture boundary changed; "
            "conservative full-suite fallback applied."
        )
        for test_id in available:
            reasons[test_id].append("full-suite fallback for unsupported boundary change")
    if uncovered:
        warnings.append(
            "Some affected source obligations have no covering test in the base profile: "
            + ", ".join(uncovered)
        )
        if mode == "conservative":
            fallback = FallbackState.FULL_SUITE
            selected = set(available)
            for test_id in available:
                reasons[test_id].append("full-suite fallback for uncovered affected code")
    if mode == "conservative" and not selected and changes:
        fallback = FallbackState.FULL_SUITE
        selected = set(available)
        warnings.append(
            "No tests mapped to semantic changes; conservative full-suite fallback applied."
        )
    return (
        TestSelection(
            mode,
            available,
            sorted(selected),
            {
                test: sorted(set(items))
                for test, items in sorted(reasons.items())
                if test in selected
            },
            fallback,
            uncovered,
            [str(coverage_path)],
        ),
        warnings,
    )


def _risk(
    changes: list[ChangedSymbol],
    impact: list[ImpactEvidence],
    selection: TestSelection,
    unresolved: int,
    runtime_supplied: bool,
) -> RiskScore:
    changed = min(len(changes) / 10.0, 1.0)
    reach = min(max(len(impact) - len(changes), 0) / 25.0, 1.0)
    coverage_gap = (
        1.0
        if selection.fallback == FallbackState.FULL_SUITE
        else min(len(selection.uncovered_obligations) / max(len(impact), 1), 1.0)
    )
    exposure = 0.5 if not runtime_supplied else 0.75
    uncertainty = min(
        (unresolved / max(len(impact), 1)) + (0.25 if not runtime_supplied else 0), 1.0
    )
    inputs = {
        "impact_size": round((changed + reach) / 2, 4),
        "coverage_gap": round(coverage_gap, 4),
        "workload_exposure": round(exposure, 4),
        "fan_in": round(reach, 4),
        "evidence_uncertainty": round(uncertainty, 4),
    }
    weights = {
        "impact_size": 25.0,
        "coverage_gap": 25.0,
        "workload_exposure": 15.0,
        "fan_in": 15.0,
        "evidence_uncertainty": 20.0,
    }
    contributions = {name: round(inputs[name] * weight, 2) for name, weight in weights.items()}
    return RiskScore(
        round(max(0.0, min(sum(contributions.values()), 100.0)), 2),
        RISK_MODEL_VERSION,
        inputs,
        weights,
        contributions,
    )


def analyze(
    repo: Path,
    base: str,
    head: str,
    coverage_path: Path | None,
    runtime_path: Path | None,
    mode: str,
) -> AnalysisReport:
    started = time.perf_counter()
    if mode not in {"conservative", "balanced", "aggressive"}:
        raise BlastRadiusError(f"unknown selection mode: {mode}")
    base_identity = resolve_revision(repo, base)
    head_identity = resolve_revision(repo, head)
    resolved_ms = (time.perf_counter() - started) * 1000
    base_index = build_index(base_identity.sha, _sources(repo, base_identity.sha))
    head_index = build_index(head_identity.sha, _sources(repo, head_identity.sha))
    indexed_ms = (time.perf_counter() - started) * 1000 - resolved_ms
    diffs = diff_files(repo, base_identity.sha, head_identity.sha)
    changes = map_changes(diffs, base_index, head_index)
    indexes = [base_index, head_index]
    runtime_mapped_edges = 0
    if runtime_path is not None:
        runtime_index = _runtime_index(runtime_path, base_identity, base_index)
        runtime_mapped_edges = len(runtime_index.edges)
        indexes.append(runtime_index)
    impact = _reverse_impact(changes, indexes)
    modified_paths = changed_paths(repo, base_identity.sha, head_identity.sha)
    changed_test_ids: set[str] = set()
    for change in changes:
        symbol = change.head
        if symbol is None:
            continue
        path = symbol.id.path
        parts = symbol.id.qualified_name.split(".")
        if (path.startswith("tests/") or Path(path).name.startswith("test_")) and any(
            part.startswith("test_") for part in parts
        ):
            changed_test_ids.add(f"{path}::{'::'.join(parts)}")
    boundary_names = {
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
    }
    unsupported_boundary = any(
        Path(path).name in boundary_names
        or Path(path).name == "conftest.py"
        or Path(path).name.startswith("requirements")
        for path in modified_paths
    )
    selection, warnings = _select_tests(
        mode,
        changes,
        impact,
        base_index,
        coverage_path,
        base_identity,
        modified_paths,
        changed_test_ids,
        unsupported_boundary,
    )
    if runtime_path is None:
        warnings.append("Runtime evidence was not supplied; static possibilities remain included.")
    elif runtime_mapped_edges == 0:
        warnings.append(
            "Runtime evidence was valid but no observed edges mapped to static symbols."
        )
    unresolved_sites = sorted(set(base_index.unresolved_calls) | set(head_index.unresolved_calls))
    uncertainty_sites = sorted(
        set(base_index.uncertainty_sites) | set(head_index.uncertainty_sites)
    )
    correspondence_uncertainty: list[str] = []
    for change in changes:
        current = change.head or change.base
        if change.ambiguity and current:
            correspondence_uncertainty.append(
                f"change_correspondence:{current.id.key}:{change.ambiguity}"
            )
    uncertainty_sites = sorted(set(uncertainty_sites) | set(correspondence_uncertainty))
    if unresolved_sites:
        warnings.append(f"Static analysis could not resolve {len(unresolved_sites)} call site(s).")
    if uncertainty_sites:
        warnings.append(
            f"Static analysis recorded {len(uncertainty_sites)} explicit uncertainty site(s)."
        )
        warnings.extend(f"Static uncertainty: {site}" for site in uncertainty_sites[:20])
        if len(uncertainty_sites) > 20:
            warnings.append(
                f"Static uncertainty: {len(uncertainty_sites) - 20} additional site(s) omitted."
            )
    risk = _risk(
        changes,
        impact,
        selection,
        len(uncertainty_sites),
        runtime_path is not None,
    )
    total_ms = (time.perf_counter() - started) * 1000
    return AnalysisReport(
        SCHEMA_VERSION,
        __version__,
        base_identity,
        head_identity,
        mode,
        changes,
        impact,
        selection,
        risk,
        sorted(set(warnings)),
        {
            "resolve_revisions": round(resolved_ms, 2),
            "static_index": round(indexed_ms, 2),
            "total": round(total_ms, 2),
        },
    )
