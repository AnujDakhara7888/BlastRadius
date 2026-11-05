"""Bounded Python call-edge tracing for explicitly selected workloads."""

from __future__ import annotations

import hashlib
import json
import platform
import runpy
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from blastradius.errors import BlastRadiusError
from blastradius.models import (
    SCHEMA_VERSION,
    DependencyEdge,
    EdgeKind,
    RuntimeProfile,
    to_primitive,
)
from blastradius.vcs.git import resolve_revision


def collect_runtime_profile(
    repo: Path, revision: str, workload: Path, output: Path
) -> RuntimeProfile:
    identity = resolve_revision(repo, revision)
    if resolve_revision(repo, "HEAD").sha != identity.sha:
        raise BlastRadiusError("runtime tracing currently requires --rev to equal checked-out HEAD")
    absolute_workload = (
        (repo / workload).resolve() if not workload.is_absolute() else workload.resolve()
    )
    try:
        absolute_workload.relative_to(repo.resolve())
    except ValueError as exc:
        raise BlastRadiusError("workload must be inside the target repository") from exc

    counts: Counter[tuple[str, str]] = Counter()
    root = str(repo.resolve())

    def function_id(frame: FrameType) -> str | None:
        filename = str(Path(frame.f_code.co_filename).resolve())
        if not filename.startswith(root + str(Path("/"))) and filename != root:
            return None
        relative = Path(filename).relative_to(repo.resolve()).as_posix()
        return f"{relative}:{frame.f_code.co_qualname}"

    def profiler(frame: FrameType, event: str, arg: Any) -> None:
        del arg
        if event != "call" or frame.f_back is None:
            return
        caller = function_id(frame.f_back)
        callee = function_id(frame)
        if caller and callee:
            counts[(caller, callee)] += 1

    old_path = list(sys.path)
    old_profile = sys.getprofile()
    module_roots = {path.stem for path in repo.glob("*.py")}
    module_roots.update(path.name for path in repo.iterdir() if (path / "__init__.py").is_file())
    saved_modules = {
        name: module
        for name, module in list(sys.modules.items())
        if name.split(".", 1)[0] in module_roots
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    try:
        sys.path.insert(0, root)
        sys.setprofile(profiler)
        runpy.run_path(str(absolute_workload), run_name="__main__")
    finally:
        sys.setprofile(old_profile)
        sys.path[:] = old_path
        for name in list(sys.modules):
            if name.split(".", 1)[0] in module_roots:
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)

    edges = [
        DependencyEdge(caller, callee, EdgeKind.RUNTIME_CALL, "representative_workload", 1.0, count)
        for (caller, callee), count in sorted(counts.items())
    ]
    payload = absolute_workload.read_bytes()
    profile = RuntimeProfile(
        SCHEMA_VERSION,
        identity,
        absolute_workload.relative_to(repo.resolve()).as_posix(),
        hashlib.sha256(payload).hexdigest(),
        "representative_workload",
        datetime.now(UTC).isoformat(),
        platform.python_version(),
        edges,
    )
    output.write_text(json.dumps(to_primitive(profile), indent=2, sort_keys=True) + "\n")
    return profile


def load_runtime_profile(path: Path) -> dict[str, Any]:
    try:
        value: dict[str, Any] = json.loads(path.read_text())
        return value
    except json.JSONDecodeError as exc:
        raise BlastRadiusError(f"invalid runtime profile {path}: {exc}") from exc
