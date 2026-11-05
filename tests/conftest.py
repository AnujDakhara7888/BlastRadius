from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "BlastRadius Tests",
            "GIT_AUTHOR_EMAIL": "tests@example.invalid",
            "GIT_COMMITTER_NAME": "BlastRadius Tests",
            "GIT_COMMITTER_EMAIL": "tests@example.invalid",
        }
    )
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.fixture
def repo_factory(tmp_path: Path) -> Callable[[dict[str, str]], Path]:
    def factory(files: dict[str, str]) -> Path:
        repo = tmp_path / "target"
        repo.mkdir()
        git(repo, "init", "-q")
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "base")
        return repo

    return factory
