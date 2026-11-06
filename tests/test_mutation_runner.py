from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import git

from blastradius.errors import BlastRadiusError
from blastradius.models import MutantGenerationReport, MutationOutcome, SuiteStatus
from blastradius.validate.mutate import generate_mutants
from blastradius.validate.mutation_runner import run_mutants, write_mutation_execution_report


def _experiment(
    repo_factory: Callable[[dict[str, str]], Path],
    tests: str,
    *,
    base_source: str = "def calculate(value):\n    return value + 1\n",
    head_source: str = "def calculate(value):\n    return value + 2\n",
    operator: str = "binary:+->-",
    source_path: str = "logic.py",
) -> tuple[Path, MutantGenerationReport]:
    repo = repo_factory({source_path: base_source, "tests/test_logic.py": tests})
    base = git(repo, "rev-parse", "HEAD")
    (repo / source_path).write_text(head_source)
    git(repo, "add", source_path)
    git(repo, "commit", "-q", "-m", "change calculation")
    generation = generate_mutants(repo, base, "HEAD")
    return repo, replace(
        generation,
        mutants=[next(mutant for mutant in generation.mutants if mutant.operator == operator)],
    )


def test_selected_and_full_use_independent_snapshots_and_actual_head(
    repo_factory: Callable[[dict[str, str]], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, generation = _experiment(
        repo_factory,
        """\
import os
from pathlib import Path
from logic import calculate

def test_smoke():
    assert os.environ.get("GITHUB_TOKEN") is None
    assert os.environ.get("PYTEST_ADDOPTS") is None
    assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    marker = Path("state.txt")
    assert not marker.exists()
    marker.write_text("created")
    assert isinstance(calculate(1), int)

def test_exact():
    assert calculate(1) == 3
""",
    )
    # Experiments read immutable objects and preserve even dirty target worktrees.
    (repo / "logic.py").write_text("raise RuntimeError('working tree must not execute')\n")
    before_status = git(repo, "status", "--porcelain=v1")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-cross-process-boundary")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--invalid-inherited-option")

    report = run_mutants(repo, generation, ["tests/test_logic.py::test_smoke"], timeout_seconds=5)

    assert all(baseline.status == SuiteStatus.PASSED for baseline in report.baselines.values())
    assert report.baselines["selected"].collected == 1
    assert report.baselines["full"].collected == 2
    result = report.results[0]
    assert result.selected.outcome == MutationOutcome.SURVIVED
    assert result.full.outcome == MutationOutcome.KILLED
    assert result.full.execution is not None
    assert result.full.execution.failed == 1
    assert not (repo / "state.txt").exists()
    assert git(repo, "status", "--porcelain=v1") == before_status
    output = tmp_path / "report.json"
    write_mutation_execution_report(report, output)
    raw = json.loads(output.read_text())
    assert raw["generation"]["head"]["sha"] == generation.head.sha
    assert raw["limits"]["timeout_seconds"] == 5
    assert raw["results"][0]["full"]["outcome"] == "killed"
    assert raw["baselines"]["selected"]["passed_tests"] == ["tests/test_logic.py::test_smoke"]


def test_failing_clean_head_is_not_a_mutant_kill(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, generation = _experiment(
        repo_factory,
        "from logic import calculate\ndef test_wrong():\n    assert calculate(1) == 99\n",
    )

    report = run_mutants(repo, generation, ["tests/test_logic.py"])

    for suite in (report.results[0].selected, report.results[0].full):
        assert suite.outcome == MutationOutcome.BASELINE_FAILED
        assert suite.execution is None
        assert "clean-head baseline" in suite.reason


def test_empty_selection_does_not_accidentally_run_full_suite(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, generation = _experiment(
        repo_factory,
        "from logic import calculate\ndef test_exact():\n    assert calculate(1) == 3\n",
    )

    report = run_mutants(repo, generation, [])

    assert report.baselines["selected"].status == SuiteStatus.NO_TESTS
    assert report.baselines["selected"].exit_code is None
    assert report.results[0].selected.outcome == MutationOutcome.INCONCLUSIVE
    assert report.results[0].selected.execution is None
    assert report.results[0].full.outcome == MutationOutcome.KILLED


@pytest.mark.parametrize(
    ("tests", "expected_status", "expected_outcome"),
    [
        ("", SuiteStatus.NO_TESTS, MutationOutcome.INCONCLUSIVE),
        (
            "import missing_dependency\n",
            SuiteStatus.INFRASTRUCTURE_ERROR,
            MutationOutcome.INFRASTRUCTURE_ERROR,
        ),
        (
            "import pytest\n@pytest.mark.skip\ndef test_skipped():\n    pass\n",
            SuiteStatus.NO_TESTS,
            MutationOutcome.INCONCLUSIVE,
        ),
        (
            "import time\ndef test_hangs():\n    time.sleep(60)\n",
            SuiteStatus.TIMEOUT,
            MutationOutcome.INCONCLUSIVE,
        ),
        (
            "import os\ndef test_exits():\n    os._exit(0)\n",
            SuiteStatus.INFRASTRUCTURE_ERROR,
            MutationOutcome.INFRASTRUCTURE_ERROR,
        ),
        (
            "def test_noisy():\n    print('x' * 1100000)\n",
            SuiteStatus.OUTPUT_LIMIT,
            MutationOutcome.INCONCLUSIVE,
        ),
    ],
)
def test_baseline_infrastructure_and_inconclusive_statuses_are_explicit(
    repo_factory: Callable[[dict[str, str]], Path],
    tests: str,
    expected_status: SuiteStatus,
    expected_outcome: MutationOutcome,
) -> None:
    repo, generation = _experiment(repo_factory, tests)

    report = run_mutants(repo, generation, ["tests/test_logic.py"], timeout_seconds=1)

    assert all(baseline.status == expected_status for baseline in report.baselines.values())
    for suite in (report.results[0].selected, report.results[0].full):
        assert suite.outcome == expected_outcome
        assert suite.execution is None


def test_mutant_timeouts_are_not_counted_as_kills(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    source = "def calculate(value):\n    if value == 1:\n        return 1\n    while True: pass\n"
    repo, generation = _experiment(
        repo_factory,
        "from logic import calculate\ndef test_exact():\n    assert calculate(2) == 1\n",
        base_source=source,
        head_source=source.replace("value == 1", "value == 2"),
        operator="compare:==->!=",
    )

    report = run_mutants(repo, generation, ["tests/test_logic.py"], timeout_seconds=1)

    assert all(baseline.status == SuiteStatus.PASSED for baseline in report.baselines.values())
    for suite in (report.results[0].selected, report.results[0].full):
        assert suite.outcome == MutationOutcome.INCONCLUSIVE
        assert suite.execution is not None
        assert suite.execution.status == SuiteStatus.TIMEOUT


def test_mutant_collection_errors_are_not_test_kills(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    source = "if 2 > 0:\n    VALUE = 3\nelse:\n    import missing_dependency\n"
    repo, generation = _experiment(
        repo_factory,
        "from logic import VALUE\ndef test_exact():\n    assert VALUE == 3\n",
        base_source=source,
        head_source=source.replace("2 > 0", "2 > 1"),
        operator="condition_negation:if",
    )

    report = run_mutants(repo, generation, ["tests/test_logic.py"])

    assert all(baseline.status == SuiteStatus.PASSED for baseline in report.baselines.values())
    for suite in (report.results[0].selected, report.results[0].full):
        assert suite.outcome == MutationOutcome.INFRASTRUCTURE_ERROR
        assert suite.execution is not None
        assert suite.execution.status == SuiteStatus.INFRASTRUCTURE_ERROR


def test_mutant_that_skips_tests_does_not_survive(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, generation = _experiment(
        repo_factory,
        """\
import pytest
from logic import calculate

def test_smoke():
    assert isinstance(calculate(1), int)

def test_may_skip():
    if calculate(1) < 0:
        pytest.skip("mutant changed the executed test set")
    assert calculate(1) == 3
""",
    )

    report = run_mutants(repo, generation, ["tests/test_logic.py"])

    for suite in (report.results[0].selected, report.results[0].full):
        assert suite.outcome == MutationOutcome.INCONCLUSIVE
        assert "different tests" in suite.reason
        assert suite.execution is not None
        assert suite.execution.skipped == 1


def test_src_layout_and_invalid_selected_node_do_not_change_full_suite(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, generation = _experiment(
        repo_factory,
        "from package.logic import calculate\ndef test_exact():\n    assert calculate(1) == 3\n",
        source_path="src/package/logic.py",
    )

    report = run_mutants(repo, generation, ["tests/test_logic.py::test_missing"])

    assert report.results[0].selected.outcome == MutationOutcome.INFRASTRUCTURE_ERROR
    assert report.results[0].selected.execution is None
    assert report.results[0].full.outcome == MutationOutcome.KILLED


def test_rejects_stale_specs_and_unsafe_inputs_before_execution(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, generation = _experiment(repo_factory, "def test_ok():\n    pass\n")
    with pytest.raises(BlastRadiusError, match="repository-relative"):
        run_mutants(repo, generation, ["../other/test.py"])
    with pytest.raises(BlastRadiusError, match="repository-relative"):
        run_mutants(repo, generation, ["--pyargs"])
    with pytest.raises(BlastRadiusError, match="timeout"):
        run_mutants(repo, generation, [], timeout_seconds=0)
    with pytest.raises(BlastRadiusError, match="identity"):
        run_mutants(repo, replace(generation, head=replace(generation.head, tree="0" * 40)), [])
    stale = replace(generation.mutants[0], source_hash="0" * 64)
    with pytest.raises(BlastRadiusError, match="source hash"):
        run_mutants(repo, replace(generation, mutants=[stale]), [])
    stale = replace(generation.mutants[0], revision=generation.base.sha)
    with pytest.raises(BlastRadiusError, match="stale"):
        run_mutants(repo, replace(generation, mutants=[stale]), [])


def test_worker_launch_failure_is_reported_as_infrastructure(
    repo_factory: Callable[[dict[str, str]], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, generation = _experiment(repo_factory, "def test_ok():\n    pass\n")
    monkeypatch.setattr("blastradius.validate.mutation_runner.sys.executable", "/missing/python")

    report = run_mutants(repo, generation, ["tests/test_logic.py"])

    assert all(
        baseline.status == SuiteStatus.INFRASTRUCTURE_ERROR
        for baseline in report.baselines.values()
    )
    assert report.results[0].full.outcome == MutationOutcome.INFRASTRUCTURE_ERROR
    assert "worker launch" in report.baselines["full"].reason


@pytest.mark.skipif(os.name != "posix", reason="process group cleanup is POSIX-specific")
def test_completed_workers_do_not_leave_running_descendants(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    children_root = tmp_path / "children"
    children_root.mkdir()
    child_code = (
        "import os, time; from pathlib import Path; "
        f"marker = Path({str(children_root)!r}) / str(os.getpid()); "
        "marker.write_text('ready'); time.sleep(1); marker.write_text('escaped')"
    )
    tests = f"""\
import subprocess
import sys
import time
from pathlib import Path

def test_child():
    child = subprocess.Popen([sys.executable, "-c", {child_code!r}])
    marker = Path({str(children_root)!r}) / str(child.pid)
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
"""
    repo, generation = _experiment(repo_factory, tests)

    report = run_mutants(repo, generation, ["tests/test_logic.py"])

    assert report.results[0].full.outcome == MutationOutcome.SURVIVED
    markers = list(children_root.iterdir())
    assert len(markers) == 4  # Two baselines and two separate mutant suites.
    time.sleep(1.1)
    assert all(marker.read_text() == "ready" for marker in markers)
