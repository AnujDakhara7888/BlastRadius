"""Collect and validate pytest-cov contexts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import coverage

from blastradius.errors import BlastRadiusError
from blastradius.models import SCHEMA_VERSION, CoverageProfile, RevisionIdentity, to_primitive
from blastradius.vcs.git import resolve_revision


def _normalize_path(repo: Path, measured: str) -> str | None:
    path = Path(measured).resolve()
    try:
        return path.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None


def collect_profile(
    repo: Path,
    revision: str,
    target: str,
    output: Path,
    pytest_args: list[str] | None = None,
) -> CoverageProfile:
    identity = resolve_revision(repo, revision)
    current = resolve_revision(repo, "HEAD")
    if current.sha != identity.sha:
        raise BlastRadiusError(
            "coverage collection currently requires --rev to equal the checked-out HEAD; "
            "use an owned checkout for another revision"
        )
    data_file = output.with_suffix(output.suffix + ".coverage")
    env = dict(os.environ)
    env["COVERAGE_FILE"] = str(data_file.resolve())
    command = [
        os.environ.get("PYTHON", sys.executable),
        "-m",
        "pytest",
        f"--cov={target}",
        "--cov-context=test",
        "--cov-report=",
        *(pytest_args or []),
    ]
    run = subprocess.run(command, cwd=repo, env=env, check=False, timeout=1800)
    if run.returncode:
        raise BlastRadiusError(f"pytest coverage collection failed with exit code {run.returncode}")

    data = coverage.CoverageData(basename=str(data_file.resolve()))
    data.read()
    test_lines: dict[str, dict[str, set[int]]] = {}
    phases: dict[str, set[str]] = {}
    unmapped: set[str] = set()
    for measured in sorted(data.measured_files()):
        relative = _normalize_path(repo, measured)
        if relative is None:
            continue
        for line, contexts in sorted(data.contexts_by_lineno(measured).items()):
            for context in contexts:
                if not context or "|" not in context:
                    if context:
                        unmapped.add(context)
                    continue
                test_id, phase = context.rsplit("|", 1)
                if phase == "call":
                    phase = "run"
                phases.setdefault(test_id, set()).add(phase)
                test_lines.setdefault(test_id, {}).setdefault(relative, set()).add(line)

    stable_lines = {
        test_id: {path: sorted(lines) for path, lines in sorted(files.items())}
        for test_id, files in sorted(test_lines.items())
    }
    fingerprint_payload = json.dumps(sorted(stable_lines), separators=(",", ":"))
    profile = CoverageProfile(
        SCHEMA_VERSION,
        identity,
        datetime.now(UTC).isoformat(),
        platform.python_version(),
        coverage.__version__,
        hashlib.sha256(fingerprint_payload.encode()).hexdigest(),
        stable_lines,
        {test_id: sorted(items) for test_id, items in sorted(phases.items())},
        sorted(unmapped),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(to_primitive(profile), indent=2, sort_keys=True) + "\n")
    data_file.unlink(missing_ok=True)
    return profile


def load_profile(path: Path) -> CoverageProfile:
    try:
        raw: dict[str, Any] = json.loads(path.read_text())
        revision = RevisionIdentity(**raw["revision"])
        return CoverageProfile(
            schema_version=raw["schema_version"],
            revision=revision,
            created_at=raw["created_at"],
            python=raw["python"],
            coverage_version=raw["coverage_version"],
            collection_fingerprint=raw["collection_fingerprint"],
            test_lines=raw["test_lines"],
            phases=raw["phases"],
            unmapped_contexts=raw.get("unmapped_contexts", []),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BlastRadiusError(f"invalid coverage profile {path}: {exc}") from exc


def validate_profile(profile: CoverageProfile, base: RevisionIdentity) -> None:
    if profile.schema_version != SCHEMA_VERSION:
        raise BlastRadiusError(
            f"coverage schema {profile.schema_version!r} is not supported; "
            f"expected {SCHEMA_VERSION!r}"
        )
    if profile.revision.repository != base.repository or profile.revision.sha != base.sha:
        raise BlastRadiusError(
            "coverage profile identity does not match the resolved base revision; recollect it"
        )
