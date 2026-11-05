from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git

from blastradius.cli import build_parser, run
from blastradius.errors import BlastRadiusError
from blastradius.models import ReplayOutcome, to_primitive
from blastradius.validate.replay import ADAPTER_VERSION, replay, write_replay_report
from blastradius.vcs.git import resolve_revision

BASE_FUNCTIONS = """\
def stable(value):
    return {"value": value * 2}

def changing(value):
    return value + 1

def raises():
    raise ValueError("base failure")

def not_json():
    return {1, 2}

def too_much_output():
    print("x" * 20000)
    return 1

def hangs():
    while True:
        pass
"""


def _write_cases(repo: Path, revision: str, output: Path, cases: list[dict[str, object]]) -> None:
    identity = resolve_revision(repo, revision)
    output.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "adapter_version": ADAPTER_VERSION,
                "revision": to_primitive(identity),
                "normalization": "exact",
                "cases": cases,
            }
        )
    )


def _case(case_id: str, function: str, *args: object) -> dict[str, object]:
    return {
        "case_id": case_id,
        "function": {"path": "functions.py", "name": function},
        "args": list(args),
        "kwargs": {},
    }


def test_replay_classifies_all_outcomes_using_actual_revisions(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"functions.py": BASE_FUNCTIONS})
    base = git(repo, "rev-parse", "HEAD")
    (repo / "functions.py").write_text(
        BASE_FUNCTIONS.replace("return value + 1", "return value + 2")
    )
    git(repo, "add", "functions.py")
    git(repo, "commit", "-q", "-m", "change behavior")
    head = git(repo, "rev-parse", "HEAD")
    cases_path = tmp_path / "cases.json"
    _write_cases(
        repo,
        base,
        cases_path,
        [
            _case("stable", "stable", 3),
            _case("changed", "changing", 3),
            _case("error", "raises"),
            _case("unsupported", "not_json"),
            _case("bounded-output", "too_much_output"),
            _case("diverged", "hangs"),
        ],
    )

    report = replay(repo, base, head, cases_path, timeout_seconds=1)
    outcomes = {result.case_id: result.outcome for result in report.results}

    assert outcomes == {
        "stable": ReplayOutcome.IDENTICAL,
        "changed": ReplayOutcome.CHANGED,
        "error": ReplayOutcome.EXECUTION_ERROR,
        "unsupported": ReplayOutcome.UNSUPPORTED,
        "bounded-output": ReplayOutcome.UNSUPPORTED,
        "diverged": ReplayOutcome.DIVERGED,
    }
    changed = next(result for result in report.results if result.case_id == "changed")
    assert changed.base_observation == {
        "status": "returned",
        "value": 4,
        "stdout": "",
        "stderr": "",
    }
    assert changed.head_observation == {
        "status": "returned",
        "value": 5,
        "stdout": "",
        "stderr": "",
    }
    assert report.capture_revision == report.base
    assert report.normalization.startswith("exact strict JSON")


def test_replay_uses_fresh_snapshot_and_sanitized_environment_per_invocation(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_factory(
        {
            "functions.py": """\
import os
from pathlib import Path

def inspect_process():
    marker = Path("state.txt")
    existed = marker.exists()
    marker.write_text("created")
    return {"existed": existed, "secret": os.environ.get("GITHUB_TOKEN")}
"""
        }
    )
    revision = git(repo, "rev-parse", "HEAD")
    cases_path = tmp_path / "cases.json"
    _write_cases(
        repo,
        revision,
        cases_path,
        [_case("first", "inspect_process"), _case("second", "inspect_process")],
    )
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-cross-boundary")

    report = replay(repo, revision, revision, cases_path)

    assert [result.outcome for result in report.results] == [
        ReplayOutcome.IDENTICAL,
        ReplayOutcome.IDENTICAL,
    ]
    assert [result.base_observation["value"] for result in report.results] == [
        {"existed": False, "secret": None},
        {"existed": False, "secret": None},
    ]


def test_replay_rejects_stale_or_unsupported_case_contracts(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"functions.py": "def stable():\n    return 1\n"})
    base = git(repo, "rev-parse", "HEAD")
    cases_path = tmp_path / "cases.json"
    _write_cases(repo, base, cases_path, [_case("nested", "Class.method")])
    raw = json.loads(cases_path.read_text())
    raw["revision"]["sha"] = "0" * 40
    cases_path.write_text(json.dumps(raw))
    with pytest.raises(BlastRadiusError, match="identity does not match"):
        replay(repo, base, base, cases_path)

    _write_cases(repo, base, cases_path, [_case("nested", "Class.method")])
    with pytest.raises(BlastRadiusError, match="module-level function name"):
        replay(repo, base, base, cases_path)


def test_replay_report_serialization_is_stable_and_explicit(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"functions.py": "def stable(value):\n    return value\n"})
    revision = git(repo, "rev-parse", "HEAD")
    cases_path = tmp_path / "cases.json"
    output = tmp_path / "replay.json"
    _write_cases(repo, revision, cases_path, [_case("same", "stable", {"x": 1})])

    report = replay(repo, revision, revision, cases_path)
    write_replay_report(report, output)
    raw = json.loads(output.read_text())

    assert raw["adapter_version"] == ADAPTER_VERSION
    assert raw["limits"]["case_count"] == 100
    assert raw["results"][0]["outcome"] == "identical"
    assert any("not automatically a regression" in warning for warning in raw["warnings"])


def test_replay_cli_writes_machine_readable_report(
    repo_factory: Callable[[dict[str, str]], Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = repo_factory({"functions.py": "def stable(value):\n    return value\n"})
    revision = git(repo, "rev-parse", "HEAD")
    cases_path = tmp_path / "cases.json"
    output = tmp_path / "replay.json"
    _write_cases(repo, revision, cases_path, [_case("same", "stable", 3)])
    args = build_parser().parse_args(
        [
            "replay",
            "--repo",
            str(repo),
            "--base",
            revision,
            "--head",
            revision,
            "--cases",
            str(cases_path),
            "--json",
            str(output),
        ]
    )

    assert run(args) == 0
    assert json.loads(output.read_text())["results"][0]["outcome"] == "identical"
    assert "identical=1" in capsys.readouterr().out


def test_replay_imports_src_layout_packages(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory(
        {
            "src/package/__init__.py": "",
            "src/package/core.py": "def stable(value):\n    return value * 2\n",
        }
    )
    revision = git(repo, "rev-parse", "HEAD")
    cases_path = tmp_path / "cases.json"
    _write_cases(repo, revision, cases_path, [_case("same", "stable", 4)])
    raw = json.loads(cases_path.read_text())
    raw["cases"][0]["function"]["path"] = "src/package/core.py"
    cases_path.write_text(json.dumps(raw))

    report = replay(repo, revision, revision, cases_path)

    assert report.results[0].outcome == ReplayOutcome.IDENTICAL
    assert report.results[0].base_observation["value"] == 8
