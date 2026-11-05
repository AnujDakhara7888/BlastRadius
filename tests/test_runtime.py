from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git
from test_analysis import BASE_SOURCE, make_change

from blastradius.errors import BlastRadiusError
from blastradius.impact.analyze import analyze
from blastradius.runtime.profile import collect_runtime_profile


def test_trace_records_labeled_scoped_edges(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory(
        {
            "pkg.py": "def leaf():\n    return 1\n\ndef entry():\n    return leaf()\n",
            "workload.py": "from pkg import entry\nentry()\n",
        }
    )
    profile_path = tmp_path / "runtime.json"
    collect_runtime_profile(repo, "HEAD", Path("workload.py"), profile_path)
    raw = json.loads(profile_path.read_text())
    assert raw["source_type"] == "representative_workload"
    assert any(edge["callee"] == "pkg.py:leaf" for edge in raw["edges"])
    assert all(edge["observed_count"] > 0 for edge in raw["edges"])


def test_stale_runtime_profile_is_rejected(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE, "workload.py": "from pkg import checkout\n"})
    base = git(repo, "rev-parse", "HEAD")
    runtime = tmp_path / "runtime.json"
    collect_runtime_profile(repo, base, Path("workload.py"), runtime)
    _, head = make_change(repo)
    raw = json.loads(runtime.read_text())
    raw["revision"]["sha"] = "0" * 40
    runtime.write_text(json.dumps(raw))
    with pytest.raises(BlastRadiusError, match="runtime profile identity"):
        analyze(repo, base, head, None, runtime, "conservative")
