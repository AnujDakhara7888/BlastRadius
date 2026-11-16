"""Bounded synthetic benchmark campaign; run with python -m blastradius.benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import coverage
import pytest

from blastradius import __version__
from blastradius.coverage.profile import collect_profile
from blastradius.errors import BlastRadiusError
from blastradius.impact.analyze import analyze
from blastradius.models import FallbackState, SuiteStatus, to_primitive
from blastradius.validate.mutate import generate_mutants
from blastradius.validate.mutation_runner import run_mutants

FIXTURE_VERSION = "independent-tests-v1"
BASE_FILES = {
    "logic.py": (
        "def calculate(value):\n    return value + 1\n\ndef inventory():\n    return 10\n"
    ),
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    "tests/test_logic.py": (
        "from logic import calculate, inventory\n\n"
        "def test_a_smoke():\n    assert isinstance(calculate(1), int)\n\n"
        "def test_z_exact():\n    assert calculate(1) in (2, 3)\n\n"
        "def test_inventory():\n    assert inventory() == 10\n"
    ),
}
HEAD_FILES = {**BASE_FILES, "logic.py": BASE_FILES["logic.py"].replace("value + 1", "value + 2")}
EXPECTED_TESTS = [
    f"tests/test_logic.py::{name}" for name in ("test_a_smoke", "test_inventory", "test_z_exact")
]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(to_primitive(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    )


def _git(repo: Path, *args: str) -> str:
    # These are explicitly synthetic fixture commits, with actual execution dates.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="BlastRadius Synthetic Benchmark",
        GIT_AUTHOR_EMAIL="benchmark@example.invalid",
        GIT_COMMITTER_NAME="BlastRadius Synthetic Benchmark",
        GIT_COMMITTER_EMAIL="benchmark@example.invalid",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _fixture(repo: Path) -> tuple[str, str]:
    repo.mkdir()
    _git(repo, "init", "-q", "--template=", "--initial-branch=main")
    revisions = []
    for label, files in (("base", BASE_FILES), ("head", HEAD_FILES)):
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        _git(repo, "add", *sorted(files))
        _git(repo, "commit", "-q", "-m", f"synthetic benchmark {label} fixture")
        revisions.append(_git(repo, "rev-parse", "HEAD"))
    return revisions[0], revisions[1]


def run_campaign(
    output: Path,
    *,
    repetitions: int = 1,
    max_mutants: int = 3,
    seed: int = 17,
    timeout_seconds: int = 10,
    mode: str = "aggressive",
) -> dict[str, Any]:
    """Retain raw evidence in a new directory; never overwrite an existing campaign."""
    if not 1 <= repetitions <= 3 or not 1 <= max_mutants <= 5:
        raise BlastRadiusError("benchmark repetitions must be 1..3 and max-mutants must be 1..5")
    if not 1 <= timeout_seconds <= 30:
        raise BlastRadiusError("benchmark timeout must be 1..30 seconds per suite")
    if mode not in {"conservative", "balanced", "aggressive"}:
        raise BlastRadiusError("unsupported benchmark selection mode")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    summary: dict[str, Any] = {
        "schema_version": "1.0",
        "tool_version": __version__,
        "started_at": started_at,
        "status": "incomplete",
        "fixture_version": FIXTURE_VERSION,
        "fixture_hashes": {
            label: {
                name: hashlib.sha256(content.encode()).hexdigest()
                for name, content in sorted(files.items())
            }
            for label, files in (("base", BASE_FILES), ("head", HEAD_FILES))
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "coverage": coverage.__version__,
            "pytest": pytest.__version__,
            "executable": sys.executable,
        },
        "parameters": {
            "repetitions": repetitions,
            "max_mutants": max_mutants,
            "seed": seed,
            "timeout_seconds": timeout_seconds,
            "mode": mode,
        },
        "limitations": [
            "One tiny synthetic fixture, not representative project performance or safety.",
            "Cold/warm refer only to the static SQLite cache, not OS or interpreter caches.",
            "Timings are raw wall-clock observations including local process overhead; "
            "no speedup claim.",
            "Coverage runs once; every repetition runs separate selected/full "
            "clean-head baselines.",
            "Mutant survivors do not prove equivalence; exclusions remain explicit.",
        ],
        "runs": [],
    }
    summary_path = output / "summary.json"
    _write_json(summary_path, summary)
    try:
        repo = output / "fixture"
        base, head = _fixture(repo)
        summary["revisions"] = {"base": base, "head": head}
        # Collection requires a checked-out base; keep all evidence outside the fixture.
        _git(repo, "checkout", "-q", base)
        profile = output / "base-coverage.json"
        stage = time.perf_counter()
        collected = collect_profile(repo, base, "logic", profile, ["tests"])
        summary["coverage_wall_ms"] = (time.perf_counter() - stage) * 1000
        observed_tests = sorted(collected.test_lines)
        coverage_complete = observed_tests == EXPECTED_TESTS
        summary["coverage_validation"] = {
            "complete": coverage_complete,
            "expected_tests": EXPECTED_TESTS,
            "observed_tests": observed_tests,
            "missing_tests": sorted(set(EXPECTED_TESTS) - set(observed_tests)),
            "unexpected_tests": sorted(set(observed_tests) - set(EXPECTED_TESTS)),
        }
        if not coverage_complete:
            summary["status"] = "invalid"
            summary["error"] = "fixture per-test coverage identities do not match expected tests"
            return summary
        _git(repo, "checkout", "-q", head)
        stage = time.perf_counter()
        generation = generate_mutants(repo, base, head, seed=seed, max_mutants=max_mutants)
        summary["generation_wall_ms"] = (time.perf_counter() - stage) * 1000
        _write_json(output / "generation.json", generation)
        valid = True
        for number in range(1, repetitions + 1):
            directory = output / f"run-{number}"
            directory.mkdir()
            cache = directory / "static.sqlite3"
            observations = []
            reports = []
            for temperature in ("cold", "warm"):
                stage = time.perf_counter()
                report = analyze(
                    repo, base, head, profile, runtime_path=None, mode=mode, cache_file=cache
                )
                wall_ms = (time.perf_counter() - stage) * 1000
                _write_json(directory / f"{temperature}.json", report)
                reports.append(report)
                observations.append(
                    {"cache": temperature, "wall_ms": wall_ms, "cache_state": report.cache_state}
                )
            cold, warm = reports
            cold_semantics, warm_semantics = to_primitive(cold), to_primitive(warm)
            for field in ("timings_ms", "cache_state"):
                cold_semantics.pop(field)
                warm_semantics.pop(field)
            matched = cold_semantics == warm_semantics
            cache_verified = (
                cold.cache_state == "base=miss;head=miss"
                and warm.cache_state == "base=hit;head=hit"
            )
            fallback = cold.selection.fallback == FallbackState.FULL_SUITE
            stage = time.perf_counter()
            execution = run_mutants(
                repo,
                generation,
                [] if fallback else cold.selection.selected_tests,
                timeout_seconds=timeout_seconds,
                selected_full_suite=fallback,
            )
            execution_ms = (time.perf_counter() - stage) * 1000
            _write_json(directory / "execution.json", execution)
            baselines_passed = all(
                baseline.status == SuiteStatus.PASSED for baseline in execution.baselines.values()
            )
            full_suite_complete = execution.baselines["full"].collected_tests == EXPECTED_TESTS
            valid = (
                valid and matched and cache_verified and baselines_passed and full_suite_complete
            )
            summary["runs"].append(
                {
                    "number": number,
                    "analysis": observations,
                    "analysis_semantics_match": matched,
                    "cache_states_verified": cache_verified,
                    "selected_tests": len(cold.selection.selected_tests),
                    "known_tests": len(cold.selection.available_tests),
                    "fallback": cold.selection.fallback.value,
                    "baselines_passed": baselines_passed,
                    "full_suite_complete": full_suite_complete,
                    "mutation_wall_ms": execution_ms,
                    "mutation_summary": to_primitive(execution.summary),
                    "evidence": {
                        name: f"run-{number}/{name}.json" for name in ("cold", "warm", "execution")
                    },
                }
            )
            _write_json(summary_path, summary)
        summary["status"] = "completed" if valid else "invalid"
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        summary["finished_at"] = datetime.now(UTC).isoformat()
        summary["total_wall_ms"] = (time.perf_counter() - started) * 1000
        _write_json(summary_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-mutants", type=int, default=3)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--timeout", type=int, default=10, help="seconds per suite")
    parser.add_argument(
        "--mode", choices=("conservative", "balanced", "aggressive"), default="aggressive"
    )
    args = parser.parse_args()
    try:
        summary = run_campaign(
            args.output,
            repetitions=args.repetitions,
            max_mutants=args.max_mutants,
            seed=args.seed,
            timeout_seconds=args.timeout,
            mode=args.mode,
        )
    except (BlastRadiusError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"benchmark error: {exc}\n")
    print(f"benchmark {summary['status']}; evidence: {args.output.resolve() / 'summary.json'}")
    raise SystemExit(0 if summary["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
