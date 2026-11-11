from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import git

from blastradius.cli import build_parser, main, run
from blastradius.models import SCHEMA_VERSION, CoverageProfile, to_primitive
from blastradius.validate.mutation import mutate
from blastradius.vcs.git import resolve_revision


def _fixture(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> tuple[Path, str, str, Path]:
    repo = repo_factory(
        {
            "logic.py": "def calculate(value):\n    return value + 1\n",
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "tests/test_logic.py": (
                "from logic import calculate\n"
                "def test_smoke():\n    assert isinstance(calculate(1), int)\n"
                "def test_exact():\n    assert calculate(1) in (2, 3)\n"
            ),
            # Explicit '.' discovery would incorrectly collect this file.
            "other/test_excluded.py": "def test_excluded():\n    assert False\n",
        }
    )
    base = git(repo, "rev-parse", "HEAD")
    profile = tmp_path / "fixture-coverage.json"
    profile.write_text(
        json.dumps(
            to_primitive(
                CoverageProfile(
                    SCHEMA_VERSION,
                    resolve_revision(repo, base),
                    datetime.now(UTC).isoformat(),
                    "3.11.0",
                    "7.10.0",
                    "synthetic selection fixture; not collected coverage",
                    {
                        "tests/test_logic.py::test_smoke": {"logic.py": [1, 2]},
                        "tests/test_logic.py::test_exact": {},
                    },
                    {"tests/test_logic.py::test_smoke": ["run"]},
                )
            )
        )
    )
    (repo / "logic.py").write_text("def calculate(value):\n    return value + 2\n")
    git(repo, "add", "logic.py")
    git(repo, "commit", "-q", "-m", "change calculation")
    return repo, base, git(repo, "rev-parse", "HEAD"), profile


def test_cli_reports_actual_selected_full_results_and_provenance(
    repo_factory: Callable[[dict[str, str]], Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, base, head, profile = _fixture(repo_factory, tmp_path)
    # Only archived head code may execute, even with an uncommitted target edit.
    (repo / "logic.py").write_text("raise RuntimeError('dirty worktree')\n")
    before = git(repo, "status", "--porcelain=v1")
    output = tmp_path / "reports" / "mutation.json"
    args = build_parser().parse_args(
        [
            "mutate",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            "HEAD",
            "--coverage",
            str(profile),
            "--mode",
            "aggressive",
            "--seed",
            "23",
            "--max-mutants",
            "3",
            "--timeout",
            "5",
            "--no-cache",
            "--json",
            str(output),
        ]
    )

    assert run(args) == 0

    raw = json.loads(output.read_text())
    analysis, execution = raw["analysis"], raw["execution"]
    assert raw["tool_version"]
    assert analysis["base"] == execution["generation"]["base"]
    assert analysis["head"] == execution["generation"]["head"]
    assert analysis["head"]["sha"] == head
    assert analysis["mode"] == "aggressive"
    assert analysis["cache_state"] == "base=disabled;head=disabled"
    assert analysis["selection"]["reasons"]["tests/test_logic.py::test_smoke"]
    assert analysis["selection"]["fallback"] == "none"
    assert execution["selected_tests"] == ["tests/test_logic.py::test_smoke"]
    assert not execution["selected_full_suite"]
    assert execution["generation"]["seed"] == 23
    assert execution["limits"]["timeout_seconds"] == 5
    assert execution["summary"]["generation"]["sampled"] == 3
    assert execution["summary"]["suites"]["selected"]["survived"] == 2
    assert execution["summary"]["suites"]["full"]["killed"] == 3
    assert execution["summary"]["comparison"]["full_only_killed"] == 2
    assert "selected=1/3" in capsys.readouterr().out
    assert git(repo, "status", "--porcelain=v1") == before


@pytest.mark.parametrize("missing_coverage", [True, False])
def test_module_cli_full_fallback_uses_configured_discovery_and_cap(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path, missing_coverage: bool
) -> None:
    repo, base, head, profile = _fixture(repo_factory, tmp_path)
    output = tmp_path / "fallback.json"
    coverage_args = []
    if not missing_coverage:
        raw = json.loads(profile.read_text())
        raw["test_lines"] = {"tests/test_logic.py::test_smoke": {}}
        profile.write_text(json.dumps(raw))
        coverage_args = ["--coverage", str(profile)]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "blastradius.cli",
            "mutate",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--max-mutants",
            "1",
            "--timeout",
            "5",
            "--no-cache",
            "--json",
            str(output),
            *coverage_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    raw = json.loads(output.read_text())
    assert raw["analysis"]["selection"]["fallback"] == "full_suite"
    execution = raw["execution"]
    assert execution["selected_full_suite"]
    assert execution["selected_tests"] == execution["full_suite"] == []
    assert execution["summary"]["generation"]["truncated"] == 2
    for baseline in execution["baselines"].values():
        assert baseline["status"] == "passed"
        assert baseline["collected"] == 2
    pair = execution["results"][0]
    assert pair["selected"]["outcome"] == pair["full"]["outcome"]
    assert "fallback=full_suite" in result.stdout


def test_cli_writes_failed_baselines_as_exclusions(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo, base, _, _ = _fixture(repo_factory, tmp_path)
    (repo / "tests/test_logic.py").write_text("def test_wrong():\n    assert False\n")
    git(repo, "add", "tests/test_logic.py")
    git(repo, "commit", "-q", "-m", "failing baseline fixture")
    output = tmp_path / "baseline-failed.json"
    args = build_parser().parse_args(
        [
            "mutate",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            "HEAD",
            "--max-mutants",
            "1",
            "--no-cache",
            "--json",
            str(output),
        ]
    )

    assert run(args) == 0

    execution = json.loads(output.read_text())["execution"]
    for summary in execution["summary"]["suites"].values():
        assert not summary["baseline_eligible"]
        assert summary["exclusions"]["baseline_failed"] == 1
        assert summary["score_denominator"] == 0
        assert summary["kill_ratio"] is None
    assert execution["summary"]["comparison"]["excluded"] == 1


def test_empty_nonfallback_selection_and_zero_mutants_are_explicit(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo, base, _, profile = _fixture(repo_factory, tmp_path)

    report = mutate(repo, base, base, profile, use_cache=False, timeout_seconds=5)

    assert report.analysis.selection.fallback.value == "none"
    assert not report.execution.selected_full_suite
    assert report.execution.selected_tests == []
    assert report.execution.baselines["selected"].status.value == "no_tests"
    assert report.execution.baselines["full"].status.value == "passed"
    assert report.execution.summary.generation.sampled == 0
    assert report.execution.summary.comparison.kill_retention_ratio is None


@pytest.mark.parametrize("invalid", ["stale", "timeout", "cap"])
def test_invalid_inputs_fail_before_target_execution_or_report_write(
    repo_factory: Callable[[dict[str, str]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid: str,
) -> None:
    repo, base, head, profile = _fixture(repo_factory, tmp_path)
    if invalid == "stale":
        raw = json.loads(profile.read_text())
        raw["revision"]["sha"] = "0" * 40
        profile.write_text(json.dumps(raw))
    output = tmp_path / "invalid.json"
    argv = [
        "blastradius",
        "mutate",
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--coverage",
        str(profile),
        "--no-cache",
        "--json",
        str(output),
    ]
    if invalid != "stale":
        argv.extend(["--timeout" if invalid == "timeout" else "--max-mutants", "0"])
    monkeypatch.setattr(sys, "argv", argv)

    def unexpected_execution(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid input must not execute target code")

    monkeypatch.setattr("blastradius.validate.mutation.run_mutants", unexpected_execution)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists()
