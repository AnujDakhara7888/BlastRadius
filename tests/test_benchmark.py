from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from blastradius.benchmark import EXPECTED_TESTS, run_campaign
from blastradius.coverage.profile import collect_profile
from blastradius.errors import BlastRadiusError
from blastradius.models import CoverageProfile, to_primitive


def test_campaign_retains_real_coverage_cache_and_matched_mutation_evidence(tmp_path: Path) -> None:
    output = tmp_path / "campaign"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "blastradius.benchmark",
            "--output",
            str(output),
            "--repetitions",
            "2",
            "--max-mutants",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
        env={**os.environ, "COVERAGE_CORE": "sysmon"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "completed"
    assert summary["environment"]["pytest"]
    assert summary["started_at"] <= summary["finished_at"]
    assert summary["total_wall_ms"] > 0
    profile = json.loads((output / "base-coverage.json").read_text())
    assert profile["revision"]["sha"] == summary["revisions"]["base"]
    assert len(profile["test_lines"]) == 3
    assert summary["coverage_validation"] == {
        "complete": True,
        "expected_tests": EXPECTED_TESTS,
        "observed_tests": EXPECTED_TESTS,
        "missing_tests": [],
        "unexpected_tests": [],
    }
    executions = []
    for run in summary["runs"]:
        assert run["analysis_semantics_match"] and run["cache_states_verified"]
        assert run["full_suite_complete"]
        assert run["selected_tests"] == 1 and run["known_tests"] == 3
        assert run["fallback"] == "none"
        assert run["mutation_summary"]["generation"] == {
            "generated": 3,
            "syntax_rejected": 0,
            "valid": 3,
            "sampled": 1,
            "truncated": 2,
        }
        execution = json.loads((output / run["evidence"]["execution"]).read_text())
        executions.append(execution)
        assert execution["generation"]["head"]["sha"] == summary["revisions"]["head"]
        assert execution["summary"] == run["mutation_summary"]
        assert execution["baselines"]["selected"]["collected"] == 1
        assert execution["baselines"]["full"]["collected"] == 3
        assert all(item["status"] == "passed" for item in execution["baselines"].values())
        assert execution["summary"]["comparison"]["comparable"] == 1
        assert all((output / path).is_file() for path in run["evidence"].values())
    assert executions[0]["generation"] == executions[1]["generation"]
    assert executions[0]["summary"] == executions[1]["summary"]
    # Existing evidence must survive a mistaken rerun verbatim.
    before = (output / "summary.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_campaign(output)
    assert (output / "summary.json").read_bytes() == before


def test_missing_coverage_context_rejects_campaign_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def incomplete(
        repo: Path, revision: str, target: str, output: Path, args: list[str]
    ) -> CoverageProfile:
        profile = collect_profile(repo, revision, target, output, args)
        del profile.test_lines["tests/test_logic.py::test_z_exact"]
        output.write_text(json.dumps(to_primitive(profile)))
        return profile

    monkeypatch.setattr("blastradius.benchmark.collect_profile", incomplete)
    output = tmp_path / "incomplete-contexts"
    summary = run_campaign(output)
    assert summary["status"] == "invalid"
    assert summary["coverage_validation"]["missing_tests"] == ["tests/test_logic.py::test_z_exact"]
    assert summary["runs"] == []
    assert summary["finished_at"]
    assert (output / "base-coverage.json").is_file()
    assert not (output / "generation.json").exists()


@pytest.mark.parametrize(
    "options",
    [
        {"repetitions": 0},
        {"repetitions": 4},
        {"max_mutants": 0},
        {"max_mutants": 6},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"mode": "invalid"},
    ],
)
def test_invalid_bounds_do_not_create_evidence(tmp_path: Path, options: dict[str, object]) -> None:
    output = tmp_path / "invalid"
    with pytest.raises(BlastRadiusError):
        run_campaign(output, **options)  # type: ignore[arg-type]
    assert not output.exists()


def test_failed_collection_retains_incomplete_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        raise BlastRadiusError("coverage collection unavailable")

    monkeypatch.setattr("blastradius.benchmark.collect_profile", fail)
    output = tmp_path / "failed"
    with pytest.raises(BlastRadiusError, match="coverage collection unavailable"):
        run_campaign(output)
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "incomplete"
    assert summary["runs"] == []
    assert summary["error"] == "BlastRadiusError: coverage collection unavailable"
    assert summary["finished_at"]
