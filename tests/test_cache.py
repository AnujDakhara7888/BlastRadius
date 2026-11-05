from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from conftest import git
from test_analysis import BASE_SOURCE, make_change, write_profile

from blastradius.impact.analyze import analyze
from blastradius.vcs.git import git_path


def test_static_cache_reports_miss_then_hit(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    profile = tmp_path / "coverage.json"
    cache = tmp_path / "cache.sqlite3"
    write_profile(repo, base, profile)
    first = analyze(repo, base, head, profile, None, "conservative", cache_file=cache)
    second = analyze(repo, base, head, profile, None, "conservative", cache_file=cache)
    assert first.cache_state == "base=miss;head=miss"
    assert second.cache_state == "base=hit;head=hit"
    assert second.selection.selected_tests == first.selection.selected_tests


def test_new_tree_invalidates_only_changed_revision_entry(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    profile = tmp_path / "coverage.json"
    cache = tmp_path / "cache.sqlite3"
    write_profile(repo, base, profile)
    analyze(repo, base, head, profile, None, "conservative", cache_file=cache)
    package = repo / "pkg.py"
    package.write_text(package.read_text().replace("round(value, 1)", "round(value, 0)"))
    git(repo, "add", "pkg.py")
    git(repo, "commit", "-q", "-m", "change rounding again")
    report = analyze(repo, base, "HEAD", profile, None, "conservative", cache_file=cache)
    assert report.cache_state == "base=hit;head=miss"


def test_corrupt_cache_rows_are_deleted_and_rebuilt(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    profile = tmp_path / "coverage.json"
    cache = tmp_path / "cache.sqlite3"
    write_profile(repo, base, profile)
    expected = analyze(repo, base, head, profile, None, "conservative", cache_file=cache)
    with sqlite3.connect(cache) as connection:
        connection.execute("UPDATE static_indexes SET payload = ?", ("not-json",))
    rebuilt = analyze(repo, base, head, profile, None, "conservative", cache_file=cache)
    assert rebuilt.cache_state == "base=corrupt_rebuilt;head=corrupt_rebuilt"
    assert rebuilt.selection.selected_tests == expected.selection.selected_tests


def test_cache_can_be_explicitly_disabled(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    profile = tmp_path / "coverage.json"
    cache = tmp_path / "must-not-exist.sqlite3"
    write_profile(repo, base, profile)
    report = analyze(
        repo,
        base,
        head,
        profile,
        None,
        "conservative",
        use_cache=False,
        cache_file=cache,
    )
    assert report.cache_state == "base=disabled;head=disabled"
    assert not cache.exists()


def test_default_cache_path_resolves_through_linked_worktree_git_dir(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "--detach", str(linked), "HEAD")

    resolved = git_path(linked, "blastradius/cache.sqlite3")

    assert resolved.name == "cache.sqlite3"
    assert resolved.parent.name == "blastradius"
    assert "worktrees" in resolved.parts
    assert not str(resolved).startswith(str(linked / ".git"))
