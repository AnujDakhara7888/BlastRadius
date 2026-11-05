"""Read immutable Git objects without touching the user's working tree."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from blastradius.errors import BlastRadiusError
from blastradius.models import RevisionIdentity

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True, slots=True)
class FileDiff:
    old_path: str | None
    new_path: str | None
    old_lines: tuple[int, ...]
    new_lines: tuple[int, ...]
    status: str


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise BlastRadiusError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def repository_identity(repo: Path) -> str:
    root = _git(repo, "rev-parse", "--show-toplevel").strip()
    remote = _git(repo, "config", "--get", "remote.origin.url", check=False).strip()
    return remote or Path(root).resolve().as_uri()


def resolve_revision(repo: Path, revision: str) -> RevisionIdentity:
    sha = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
    tree = _git(repo, "rev-parse", "--verify", f"{sha}^{{tree}}").strip()
    return RevisionIdentity(repository_identity(repo), sha, tree)


def list_python_files(repo: Path, revision: str) -> list[str]:
    output = _git(repo, "ls-tree", "-r", "--name-only", revision)
    return sorted(path for path in output.splitlines() if path.endswith(".py"))


def read_file(repo: Path, revision: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout if result.returncode == 0 else None


def diff_files(repo: Path, base: str, head: str) -> list[FileDiff]:
    names = _git(repo, "diff", "--name-status", "--find-renames", base, head)
    status_rows: list[tuple[str, str | None, str | None]] = []
    for row in names.splitlines():
        parts = row.split("\t")
        status = parts[0]
        if status.startswith("R") and len(parts) == 3:
            status_rows.append(("R", parts[1], parts[2]))
        elif len(parts) == 2:
            path = parts[1]
            status_rows.append(
                (
                    status[0],
                    None if status.startswith("A") else path,
                    None if status.startswith("D") else path,
                )
            )

    result: list[FileDiff] = []
    for status, old_path, new_path in status_rows:
        relevant = (old_path and old_path.endswith(".py")) or (
            new_path and new_path.endswith(".py")
        )
        if not relevant:
            continue
        patch = _git(
            repo,
            "diff",
            "--unified=0",
            "--no-ext-diff",
            base,
            head,
            "--",
            old_path or new_path or "",
            new_path or old_path or "",
        )
        old_lines: set[int] = set()
        new_lines: set[int] = set()
        for line in patch.splitlines():
            match = HUNK_RE.match(line)
            if not match:
                continue
            old_start, old_count, new_start, new_count = match.groups()
            old_lines.update(range(int(old_start), int(old_start) + int(old_count or "1")))
            new_lines.update(range(int(new_start), int(new_start) + int(new_count or "1")))
        result.append(
            FileDiff(old_path, new_path, tuple(sorted(old_lines)), tuple(sorted(new_lines)), status)
        )
    return sorted(result, key=lambda item: item.new_path or item.old_path or "")


def changed_paths(repo: Path, base: str, head: str) -> set[str]:
    """Return every changed path, including non-Python policy boundaries."""
    output = _git(repo, "diff", "--name-only", "--find-renames", base, head)
    return {line.strip() for line in output.splitlines() if line.strip()}


def git_path(repo: Path, name: str) -> Path:
    """Resolve a Git-owned path correctly for normal and linked worktrees."""
    value = Path(_git(repo, "rev-parse", "--git-path", name).strip())
    return value if value.is_absolute() else (repo / value).resolve()
