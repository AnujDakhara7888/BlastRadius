"""Local PR event analysis: immutable Git reads and artifacts, without GitHub writes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from blastradius.errors import BlastRadiusError
from blastradius.impact.analyze import analyze
from blastradius.models import AnalysisReport, to_primitive
from blastradius.report.html import write_html
from blastradius.report.render import write_json, write_markdown
from blastradius.vcs.git import repository_identity

MAX_EVENT_BYTES = 1_048_576


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise BlastRadiusError("PR base/head must be full lowercase Git commit SHAs")
    return value


def write_github_artifacts(
    repo: Path,
    event_path: Path,
    output: Path,
    coverage_path: Path | None = None,
    runtime_path: Path | None = None,
    mode: str = "conservative",
) -> AnalysisReport:
    """Read a pull_request event and analyze its exact base/head, never its merge ref."""
    if output.exists():
        raise BlastRadiusError("artifact output directory already exists; use a new directory")
    try:
        with event_path.open("rb") as stream:
            event_bytes = stream.read(MAX_EVENT_BYTES + 1)
        if len(event_bytes) > MAX_EVENT_BYTES:
            raise BlastRadiusError("GitHub event exceeds the 1 MiB limit")
        event: dict[str, Any] = json.loads(event_bytes)
        number = event["number"]
        name = event["repository"]["full_name"]
        pr = event["pull_request"]
        base, head = _sha(pr["base"]["sha"]), _sha(pr["head"]["sha"])
        if type(number) is not int or number <= 0:
            raise BlastRadiusError("GitHub event requires a positive PR number")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) is None
        ):
            raise BlastRadiusError("GitHub event requires an owner/repository full_name")
        if pr["base"]["repo"]["full_name"] != name:
            raise BlastRadiusError("PR base repository does not match the event repository")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BlastRadiusError(f"invalid pull_request event: {exc}") from exc

    identity = repository_identity(repo)
    remote = identity.removesuffix(".git").lower()
    if remote not in {
        f"https://github.com/{name.lower()}",
        f"git@github.com:{name.lower()}",
        f"ssh://git@github.com/{name.lower()}",
    }:
        raise BlastRadiusError("local origin does not match the GitHub event repository")
    # No checkout, import, pytest, hook, or target dependency installation. Disable
    # the Git-owned cache too: the only writes belong in the requested artifact directory.
    report = analyze(repo, base, head, coverage_path, runtime_path, mode, use_cache=False)
    if report.base.sha != base or report.head.sha != head:
        raise BlastRadiusError("resolved revisions do not match the event commit SHAs")
    try:
        output.mkdir(parents=True, exist_ok=False)
        write_json(report, output / "analysis.json")
        write_markdown(report, output / "analysis.md")
        write_html(report, output / "analysis.html")
        files = ("analysis.json", "analysis.md", "analysis.html")
        manifest = {
            "schema_version": "1.0",
            "kind": "github-pr-analysis-artifacts",
            "pull_request": {"number": number, "repository": name},
            "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "base": to_primitive(report.base),
            "head": to_primitive(report.head),
            "tool_version": report.tool_version,
            "files": {
                name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in files
            },
            "limitations": [
                "No target code or tests were executed; "
                "this is static analysis with optional profiles.",
                "No GitHub API requests, comments, check runs, "
                "or GitHub repository writes were performed.",
                "Missing coverage requires full-suite fallback; "
                "known tests are not full discovery.",
                "Risk is a heuristic prioritization score, not a failure probability.",
            ],
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise BlastRadiusError(f"cannot write PR artifacts: {exc}") from exc
    return report
