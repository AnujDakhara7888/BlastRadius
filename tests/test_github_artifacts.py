from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git

from blastradius.cli import build_parser, run
from blastradius.errors import BlastRadiusError
from blastradius.github_artifacts import MAX_EVENT_BYTES, write_github_artifacts


def _fixture(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> tuple[Path, str, str, Path]:
    repo = repo_factory(
        {
            "logic.py": (
                "from pathlib import Path\n"
                "Path('executed-target').touch()\n"
                "def answer():\n    return 1\n"
            ),
            "conftest.py": "raise RuntimeError('must not execute pytest')\n",
            "setup.py": "raise RuntimeError('must not install target')\n",
        }
    )
    git(repo, "remote", "add", "origin", "https://github.com/owner/project.git")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "logic.py").write_text((repo / "logic.py").read_text().replace("return 1", "return 2"))
    git(repo, "add", "logic.py")
    git(repo, "commit", "-q", "-m", "head")
    head = git(repo, "rev-parse", "HEAD")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "number": 7,
                "repository": {"full_name": "owner/project"},
                "pull_request": {
                    "base": {"sha": base, "repo": {"full_name": "owner/project"}},
                    "head": {"sha": head, "repo": {"full_name": "fork/project"}},
                    "title": "<script>ignored untrusted metadata</script>",
                },
            }
        )
    )
    return repo, base, head, event


def test_cli_artifacts_use_event_pins_preserve_target_and_bind_file_hashes(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo, base, head, event = _fixture(repo_factory, tmp_path)
    # The checkout may be a merge ref or an unrelated later commit; neither is the PR head.
    (repo / "logic.py").write_text("raise RuntimeError('unrelated current checkout')\n")
    git(repo, "add", "logic.py")
    git(repo, "commit", "-q", "-m", "unrelated checkout")
    (repo / "logic.py").write_text("uncommitted owner work\n")
    before = git(repo, "status", "--porcelain=v1")
    output = tmp_path / "artifacts"
    args = build_parser().parse_args(
        ["github-artifacts", "--repo", str(repo), "--event", str(event), "--output", str(output)]
    )

    assert run(args) == 0

    report = json.loads((output / "analysis.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert report["base"]["sha"] == base
    assert report["head"]["sha"] == head != git(repo, "rev-parse", "HEAD")
    assert report["selection"]["fallback"] == "full_suite"
    assert report["cache_state"] == "base=disabled;head=disabled"
    assert manifest["base"] == report["base"]
    assert manifest["head"] == report["head"]
    assert manifest["pull_request"] == {"number": 7, "repository": "owner/project"}
    assert manifest["event_sha256"] == hashlib.sha256(event.read_bytes()).hexdigest()
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert set(p.name for p in output.iterdir()) == {
        "analysis.json",
        "analysis.md",
        "analysis.html",
        "manifest.json",
    }
    assert "No target code or tests were executed" in manifest["limitations"][0]
    assert git(repo, "status", "--porcelain=v1") == before
    assert (repo / "logic.py").read_text() == "uncommitted owner work\n"
    assert not (repo / "executed-target").exists()
    assert not (repo / ".git/blastradius").exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "branch-ref",
        "option-ref",
        "short-sha",
        "missing-pr",
        "wrong-base-repository",
        "wrong-origin",
        "boolean-number",
        "not-an-object",
        "malformed",
        "oversized",
        "unavailable-head",
    ],
)
def test_invalid_event_or_missing_git_objects_never_publish_artifacts(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path, invalid: str
) -> None:
    repo, _, _, event = _fixture(repo_factory, tmp_path)
    raw = json.loads(event.read_text())
    if invalid in {"branch-ref", "option-ref", "short-sha", "unavailable-head"}:
        raw["pull_request"]["head"]["sha"] = {
            "branch-ref": "HEAD",
            "option-ref": "--help",
            "short-sha": "abcd123",
            "unavailable-head": "f" * 40,
        }[invalid]
    elif invalid == "missing-pr":
        del raw["pull_request"]
    elif invalid == "wrong-base-repository":
        raw["pull_request"]["base"]["repo"]["full_name"] = "elsewhere/project"
    elif invalid == "wrong-origin":
        git(repo, "remote", "set-url", "origin", "https://github.com/elsewhere/project.git")
    elif invalid == "boolean-number":
        raw["number"] = True
    event.write_text(json.dumps(raw))
    if invalid == "not-an-object":
        event.write_text("null")
    elif invalid == "malformed":
        event.write_bytes(b"\xff")
    elif invalid == "oversized":
        event.write_bytes(b"x" * (MAX_EVENT_BYTES + 1))
    output = tmp_path / "artifacts"
    with pytest.raises(BlastRadiusError):
        write_github_artifacts(repo, event, output)
    assert not output.exists()
    assert not (repo / "executed-target").exists()


def test_existing_artifacts_are_not_overwritten(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo, _, _, event = _fixture(repo_factory, tmp_path)
    output = tmp_path / "artifacts"
    output.mkdir()
    sentinel = output / "analysis.json"
    sentinel.write_text("previous attempt")
    with pytest.raises(BlastRadiusError, match="already exists"):
        write_github_artifacts(repo, event, output)
    assert sentinel.read_text() == "previous attempt"
