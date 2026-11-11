"""Public mutation workflow tied to fresh revision and test-selection evidence."""

from __future__ import annotations

import json
from pathlib import Path

from blastradius import __version__
from blastradius.errors import BlastRadiusError
from blastradius.impact.analyze import analyze
from blastradius.models import (
    SCHEMA_VERSION,
    FallbackState,
    MutationWorkflowReport,
    to_primitive,
)
from blastradius.validate.mutate import DEFAULT_MAX_MUTANTS, generate_mutants
from blastradius.validate.mutation_runner import run_mutants


def mutate(
    repo: Path,
    base: str,
    head: str,
    coverage_path: Path | None = None,
    runtime_path: Path | None = None,
    mode: str = "conservative",
    *,
    seed: int = 17,
    max_mutants: int = DEFAULT_MAX_MUTANTS,
    timeout_seconds: int = 30,
    use_cache: bool = True,
    cache_file: Path | None = None,
) -> MutationWorkflowReport:
    """Compare analysis-selected tests with default pytest discovery on immutable head.

    A full-suite fallback uses the same default discovery for both suites. An empty selection
    without fallback stays empty. Reports retain the complete analysis, including reasons and
    uncovered/truncated obligations, alongside each execution and its explicit denominator.
    """
    if not 1 <= timeout_seconds <= 1800:
        raise BlastRadiusError("mutation timeout must be between 1 and 1800 seconds")
    generation = generate_mutants(repo, base, head, seed=seed, max_mutants=max_mutants)
    # Resolve symbolic revisions only once, then analyze exactly the generated revision pair.
    analysis = analyze(
        repo,
        generation.base.sha,
        generation.head.sha,
        coverage_path,
        runtime_path,
        mode,
        use_cache=use_cache,
        cache_file=cache_file,
    )
    fallback = analysis.selection.fallback == FallbackState.FULL_SUITE
    execution = run_mutants(
        repo,
        generation,
        [] if fallback else analysis.selection.selected_tests,
        timeout_seconds=timeout_seconds,
        selected_full_suite=fallback,
    )
    return MutationWorkflowReport(SCHEMA_VERSION, __version__, analysis, execution)


def write_mutation_report(report: MutationWorkflowReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(to_primitive(report), indent=2, sort_keys=True) + "\n")
