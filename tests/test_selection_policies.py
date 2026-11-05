from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git

from blastradius.impact.analyze import analyze
from blastradius.models import SCHEMA_VERSION, FallbackState
from blastradius.vcs.git import resolve_revision

CHAIN = """\
def leaf():
    return 1

def middle():
    return leaf()

def top():
    return middle()

def far():
    return top()

def unrelated():
    return 0
"""


def _profile(repo: Path, base: str, output: Path) -> None:
    identity = resolve_revision(repo, base)
    tests = {
        "tests/test_chain.py::test_all": {"chain.py": [2, 5, 8, 11]},
        "tests/test_chain.py::test_far": {"chain.py": [11]},
        "tests/test_chain.py::test_leaf": {"chain.py": [2]},
        "tests/test_chain.py::test_middle": {"chain.py": [5]},
        "tests/test_chain.py::test_top": {"chain.py": [8]},
        "tests/test_chain.py::test_unrelated": {"chain.py": [14]},
    }
    output.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "revision": {
                    "repository": identity.repository,
                    "sha": identity.sha,
                    "tree": identity.tree,
                },
                "created_at": "2026-01-01T00:00:00+00:00",
                "python": "3.12.0",
                "coverage_version": "7.10.0",
                "collection_fingerprint": "policy-fixture",
                "test_lines": tests,
                "phases": {test_id: ["run"] for test_id in tests},
                "unmapped_contexts": [],
            }
        )
    )


def _changed_repo(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> tuple[Path, str, str, Path]:
    repo = repo_factory({"chain.py": CHAIN})
    base = git(repo, "rev-parse", "HEAD")
    profile = tmp_path / "coverage.json"
    _profile(repo, base, profile)
    (repo / "chain.py").write_text(CHAIN.replace("return 1", "return 2"))
    git(repo, "add", "chain.py")
    git(repo, "commit", "-q", "-m", "change leaf")
    return repo, base, git(repo, "rev-parse", "HEAD"), profile


def test_conservative_is_union_of_all_covering_tests(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo, base, head, profile = _changed_repo(repo_factory, tmp_path)
    report = analyze(repo, base, head, profile, None, "conservative")
    assert report.selection.fallback == FallbackState.NONE
    assert report.selection.selected_tests == [
        "tests/test_chain.py::test_all",
        "tests/test_chain.py::test_far",
        "tests/test_chain.py::test_leaf",
        "tests/test_chain.py::test_middle",
        "tests/test_chain.py::test_top",
    ]
    assert report.selection.truncated_obligations == []
    assert "conservative:union" in report.selection.provenance


def test_balanced_bounds_transitive_depth_and_reports_truncation(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo, base, head, profile = _changed_repo(repo_factory, tmp_path)
    report = analyze(repo, base, head, profile, None, "balanced")
    assert report.selection.selected_tests == [
        "tests/test_chain.py::test_all",
        "tests/test_chain.py::test_leaf",
        "tests/test_chain.py::test_middle",
        "tests/test_chain.py::test_top",
    ]
    assert report.selection.truncated_obligations == ["symbol:chain.py:far:function:0"]
    assert any("dependency depth 2" in warning for warning in report.warnings)
    assert "balanced:max_dependency_depth=2" in report.selection.provenance


def test_aggressive_greedy_set_cover_is_deterministic_and_explicit(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo, base, head, profile = _changed_repo(repo_factory, tmp_path)
    first = analyze(repo, base, head, profile, None, "aggressive")
    second = analyze(repo, base, head, profile, None, "aggressive")
    assert first.selection.selected_tests == ["tests/test_chain.py::test_all"]
    assert second.selection.selected_tests == first.selection.selected_tests
    assert first.selection.uncovered_obligations == []
    assert any("not mathematically minimal" in warning for warning in first.warnings)
    assert "aggressive:deterministic_greedy_set_cover" in first.selection.provenance


@pytest.mark.parametrize("mode", ["conservative", "balanced", "aggressive"])
def test_shared_fixture_boundary_forces_full_suite_in_every_mode(
    mode: str,
    repo_factory: Callable[[dict[str, str]], Path],
    tmp_path: Path,
) -> None:
    repo = repo_factory({"chain.py": CHAIN, "tests/conftest.py": "VALUE = 1\n"})
    base = git(repo, "rev-parse", "HEAD")
    profile = tmp_path / f"{mode}.json"
    _profile(repo, base, profile)
    (repo / "tests" / "conftest.py").write_text("VALUE = 2\n")
    git(repo, "add", "tests/conftest.py")
    git(repo, "commit", "-q", "-m", "change shared fixture")
    report = analyze(repo, base, "HEAD", profile, None, mode)
    assert report.selection.fallback == FallbackState.FULL_SUITE
    assert set(report.selection.available_tests) <= set(report.selection.selected_tests)
