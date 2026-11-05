from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git

from blastradius.errors import BlastRadiusError
from blastradius.impact.analyze import analyze
from blastradius.models import SCHEMA_VERSION, ChangeKind, FallbackState
from blastradius.vcs.git import resolve_revision

BASE_SOURCE = """\
def price(value):
    return round(value, 2)

def checkout(value):
    return price(value)

def unrelated():
    return 42
"""


def make_change(repo: Path) -> tuple[str, str]:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "pkg.py").write_text(BASE_SOURCE.replace("round(value, 2)", "round(value, 1)"))
    git(repo, "add", "pkg.py")
    git(repo, "commit", "-q", "-m", "change rounding")
    return base, git(repo, "rev-parse", "HEAD")


def write_profile(repo: Path, base: str, path: Path, *, stale: bool = False) -> None:
    identity = resolve_revision(repo, base)
    revision = {
        "repository": identity.repository,
        "sha": "0" * 40 if stale else identity.sha,
        "tree": identity.tree,
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "revision": revision,
                "created_at": "2026-01-01T00:00:00+00:00",
                "python": "3.12.0",
                "coverage_version": "7.10.0",
                "collection_fingerprint": "fixture",
                "test_lines": {
                    "tests/test_pkg.py::test_checkout": {"pkg.py": [1, 2, 4, 5]},
                    "tests/test_pkg.py::test_unrelated": {"pkg.py": [7, 8]},
                },
                "phases": {
                    "tests/test_pkg.py::test_checkout": ["run"],
                    "tests/test_pkg.py::test_unrelated": ["run"],
                },
                "unmapped_contexts": [],
            }
        )
    )


def test_end_to_end_selects_direct_and_dependent_coverage(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    profile = tmp_path / "coverage.json"
    write_profile(repo, base, profile)
    report = analyze(repo, base, head, profile, None, "conservative")
    assert report.selection.fallback == FallbackState.NONE
    assert report.selection.selected_tests == ["tests/test_pkg.py::test_checkout"]
    affected = {item.affected_symbol for item in report.impact}
    assert "pkg.py:checkout:function:0" in affected
    assert report.risk.label.startswith("heuristic")
    assert 0 <= report.risk.score <= 100


def test_missing_coverage_is_explicit_full_suite_fallback(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    report = analyze(repo, base, head, None, None, "conservative")
    assert report.selection.fallback == FallbackState.FULL_SUITE
    assert any("Coverage evidence is missing" in item for item in report.warnings)


def test_stale_profile_is_rejected(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    profile = tmp_path / "coverage.json"
    write_profile(repo, base, profile, stale=True)
    with pytest.raises(BlastRadiusError, match="does not match"):
        analyze(repo, base, head, profile, None, "conservative")


def test_uncovered_obligation_forces_all_available_tests(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    profile = tmp_path / "coverage.json"
    write_profile(repo, base, profile)
    raw = json.loads(profile.read_text())
    raw["test_lines"]["tests/test_pkg.py::test_checkout"] = {"pkg.py": [4, 5]}
    profile.write_text(json.dumps(raw))
    report = analyze(repo, base, head, profile, None, "conservative")
    assert report.selection.fallback == FallbackState.FULL_SUITE
    assert report.selection.selected_tests == sorted(raw["test_lines"])


def test_shared_fixture_change_forces_conservative_fallback(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE, "tests/conftest.py": "VALUE = 1\n"})
    base = git(repo, "rev-parse", "HEAD")
    profile = tmp_path / "coverage.json"
    write_profile(repo, base, profile)
    (repo / "tests" / "conftest.py").write_text("VALUE = 2\n")
    git(repo, "add", "tests/conftest.py")
    git(repo, "commit", "-q", "-m", "change shared fixture")
    head = git(repo, "rev-parse", "HEAD")
    report = analyze(repo, base, head, profile, None, "conservative")
    assert report.selection.fallback == FallbackState.FULL_SUITE
    assert report.selection.selected_tests == sorted(report.selection.available_tests)
    assert any("shared-fixture boundary" in warning for warning in report.warnings)


def test_new_test_is_included_even_when_absent_from_base_profile(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base = git(repo, "rev-parse", "HEAD")
    profile = tmp_path / "coverage.json"
    write_profile(repo, base, profile)
    test_path = repo / "tests" / "test_new.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_new_behavior():\n    assert True\n")
    git(repo, "add", "tests/test_new.py")
    git(repo, "commit", "-q", "-m", "add regression test")
    head = git(repo, "rev-parse", "HEAD")
    report = analyze(repo, base, head, profile, None, "conservative")
    assert "tests/test_new.py::test_new_behavior" in report.selection.selected_tests


def test_renamed_function_keeps_base_dependents_in_impact_and_selection(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    source = (
        "def old_price(value):\n"
        "    return round(value, 2)\n\n"
        "def checkout(value):\n"
        "    return old_price(value)\n"
    )
    repo = repo_factory({"shop.py": source})
    base = git(repo, "rev-parse", "HEAD")
    profile = tmp_path / "coverage.json"
    identity = resolve_revision(repo, base)
    profile.write_text(
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
                "collection_fingerprint": "rename-fixture",
                "test_lines": {
                    "tests/test_shop.py::test_checkout": {"shop.py": [2, 5]},
                    "tests/test_shop.py::test_other": {"shop.py": []},
                },
                "phases": {
                    "tests/test_shop.py::test_checkout": ["run"],
                    "tests/test_shop.py::test_other": ["run"],
                },
                "unmapped_contexts": [],
            }
        )
    )
    (repo / "shop.py").write_text(source.replace("old_price", "new_price"))
    git(repo, "add", "shop.py")
    git(repo, "commit", "-q", "-m", "rename price helper")
    head = git(repo, "rev-parse", "HEAD")
    report = analyze(repo, base, head, profile, None, "conservative")
    renamed = next(
        change for change in report.changed_symbols if change.change == ChangeKind.RENAMED
    )
    assert renamed.base is not None and renamed.base.id.qualified_name == "old_price"
    assert renamed.head is not None and renamed.head.id.qualified_name == "new_price"
    assert any(
        finding.affected_symbol == "shop.py:checkout:function:0" for finding in report.impact
    )
    assert report.selection.selected_tests == ["tests/test_shop.py::test_checkout"]


def test_pure_file_move_uses_symbol_coverage_without_empty_region_fallback(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"old.py": "def helper():\n    return 1\n"})
    base = git(repo, "rev-parse", "HEAD")
    identity = resolve_revision(repo, base)
    profile = tmp_path / "move-coverage.json"
    profile.write_text(
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
                "collection_fingerprint": "move-fixture",
                "test_lines": {
                    "tests/test_old.py::test_helper": {"old.py": [2]},
                    "tests/test_old.py::test_other": {"old.py": []},
                },
                "phases": {
                    "tests/test_old.py::test_helper": ["run"],
                    "tests/test_old.py::test_other": ["run"],
                },
                "unmapped_contexts": [],
            }
        )
    )
    (repo / "old.py").rename(repo / "new.py")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "move helper module")
    report = analyze(repo, base, "HEAD", profile, None, "conservative")
    helper_move = next(
        change
        for change in report.changed_symbols
        if change.head and change.head.id.qualified_name == "helper"
    )
    assert helper_move.change == ChangeKind.RENAMED
    assert helper_move.base is not None and helper_move.base.id.path == "old.py"
    assert helper_move.head is not None and helper_move.head.id.path == "new.py"
    assert report.selection.fallback == FallbackState.NONE
    assert report.selection.selected_tests == ["tests/test_old.py::test_helper"]
