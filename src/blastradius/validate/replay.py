"""Bounded differential replay for explicit JSON calls to module-level functions."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from blastradius.errors import BlastRadiusError
from blastradius.models import (
    SCHEMA_VERSION,
    ReplayCase,
    ReplayOutcome,
    ReplayReport,
    ReplayResult,
    RevisionIdentity,
    to_primitive,
)
from blastradius.vcs.git import resolve_revision

ADAPTER_VERSION = "json-call-v1"
INPUT_SCHEMA = "blastradius.replay-cases/v1"
MAX_CASES = 100
MAX_CASE_FILE_BYTES = 1_048_576
MAX_CASE_BYTES = 65_536
MAX_WORKER_OUTPUT_BYTES = 1_048_576
MAX_ARCHIVE_BYTES = 52_428_800
MAX_ARCHIVE_ENTRIES = 10_000
DEFAULT_TIMEOUT_SECONDS = 5
_FUNCTION_RE = re.compile(r"^[A-Za-z_]\w*$")


def _strict_loads(payload: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r} is not supported")

    return json.loads(payload, parse_constant=reject_constant)


def _load_cases(path: Path, base: RevisionIdentity) -> list[ReplayCase]:
    try:
        if path.stat().st_size > MAX_CASE_FILE_BYTES:
            raise BlastRadiusError(f"replay cases file exceeds {MAX_CASE_FILE_BYTES} bytes")
        raw = _strict_loads(path.read_text())
        if not isinstance(raw, dict):
            raise TypeError("top-level value must be an object")
        if raw["schema_version"] != SCHEMA_VERSION or raw["adapter_version"] != ADAPTER_VERSION:
            raise BlastRadiusError("replay cases schema or adapter version is not supported")
        if raw.get("normalization", "exact") != "exact":
            raise BlastRadiusError("only exact replay normalization is supported")
        capture_revision = RevisionIdentity(**raw["revision"])
        if capture_revision != base:
            raise BlastRadiusError(
                "replay cases identity does not match the resolved base revision; recapture cases"
            )
        raw_cases = raw["cases"]
        if not isinstance(raw_cases, list):
            raise TypeError("cases must be an array")
        if len(raw_cases) > MAX_CASES:
            raise BlastRadiusError(f"replay cases exceed the {MAX_CASES}-case sample cap")
        cases: list[ReplayCase] = []
        seen: set[str] = set()
        for item in raw_cases:
            if not isinstance(item, dict):
                raise TypeError("each case must be an object")
            case_id = item["case_id"]
            function = item["function"]
            args = item.get("args", [])
            kwargs = item.get("kwargs", {})
            if not isinstance(case_id, str) or not case_id or len(case_id) > 128:
                raise TypeError("case_id must be a non-empty string of at most 128 characters")
            if case_id in seen:
                raise BlastRadiusError(f"duplicate replay case_id {case_id!r}")
            seen.add(case_id)
            if not isinstance(function, dict):
                raise TypeError("function must be an object")
            source_path = function["path"]
            function_name = function["name"]
            if not isinstance(source_path, str) or not isinstance(function_name, str):
                raise TypeError("function path and name must be strings")
            posix_path = PurePosixPath(source_path)
            if (
                posix_path.is_absolute()
                or ".." in posix_path.parts
                or posix_path.suffix != ".py"
                or not posix_path.parts
            ):
                raise BlastRadiusError(f"unsupported replay function path {source_path!r}")
            module_parts = list(posix_path.with_suffix("").parts)
            if posix_path.parts[0] == "src" and len(posix_path.parts) > 1:
                module_parts.pop(0)
            if module_parts[-1] == "__init__":
                module_parts.pop()
            if not module_parts or any(not part.isidentifier() for part in module_parts):
                raise BlastRadiusError(
                    f"replay function path is not an importable module: {source_path!r}"
                )
            if not _FUNCTION_RE.fullmatch(function_name):
                raise BlastRadiusError(
                    "replay supports only a single module-level function name; "
                    f"got {function_name!r}"
                )
            if not isinstance(args, list) or not isinstance(kwargs, dict):
                raise TypeError("args must be an array and kwargs must be an object")
            encoded = json.dumps(item, sort_keys=True, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode()) > MAX_CASE_BYTES:
                raise BlastRadiusError(f"replay case {case_id!r} exceeds {MAX_CASE_BYTES} bytes")
            cases.append(ReplayCase(case_id, source_path, function_name, tuple(args), kwargs))
        return cases
    except BlastRadiusError:
        raise
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BlastRadiusError(f"invalid replay cases {path}: {exc}") from exc


def _archive(repo: Path, revision: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", revision],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise BlastRadiusError(f"git archive failed for {revision}: {detail}")
    if len(result.stdout) > MAX_ARCHIVE_BYTES:
        raise BlastRadiusError(f"revision archive exceeds {MAX_ARCHIVE_BYTES} bytes")
    return result.stdout


def _extract_archive(payload: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_ENTRIES:
            raise BlastRadiusError(
                f"revision archive exceeds the {MAX_ARCHIVE_ENTRIES}-entry limit"
            )
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise BlastRadiusError(f"unsafe path in revision archive: {member.name!r}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise BlastRadiusError(
                    f"unsupported non-regular entry in revision archive: {member.name!r}"
                )
            source = archive.extractfile(member)
            if source is None:
                raise BlastRadiusError(f"could not read revision archive entry {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())


def _worker_environment(home: Path) -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update(
        {
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "BLASTRADIUS_REPLAY": "1",
        }
    )
    return environment


def _invoke(
    root: Path, case: ReplayCase, timeout_seconds: int, home: Path
) -> tuple[dict[str, Any], float]:
    worker = Path(__file__).with_name("_replay_worker.py")
    invocation = json.dumps(
        {"args": list(case.args), "kwargs": case.kwargs},
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    started = time.perf_counter()
    try:
        run = subprocess.run(
            [
                sys.executable,
                "-I",
                str(worker),
                str(root),
                case.path,
                case.function,
            ],
            input=invocation,
            text=True,
            capture_output=True,
            cwd=root,
            env=_worker_environment(home),
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        duration = (time.perf_counter() - started) * 1000
        return {"status": "diverged", "reason": "worker timed out"}, duration
    duration = (time.perf_counter() - started) * 1000
    if len(run.stdout.encode()) > MAX_WORKER_OUTPUT_BYTES:
        return {"status": "diverged", "reason": "worker output exceeded limit"}, duration
    if run.returncode:
        return {
            "status": "diverged",
            "reason": f"worker exited with status {run.returncode}",
        }, duration
    try:
        observation = _strict_loads(run.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"status": "diverged", "reason": "worker returned invalid JSON"}, duration
    if not isinstance(observation, dict) or not isinstance(observation.get("status"), str):
        return {"status": "diverged", "reason": "worker returned an invalid observation"}, duration
    return observation, duration


def _classify(
    case: ReplayCase,
    base_observation: dict[str, Any],
    head_observation: dict[str, Any],
    durations: dict[str, float],
) -> ReplayResult:
    observations = (base_observation, head_observation)
    unsupported = [
        str(item.get("reason", "unsupported call"))
        for item in observations
        if item["status"] == "unsupported"
    ]
    if unsupported:
        return ReplayResult(
            case.case_id,
            case.function_id,
            ReplayOutcome.UNSUPPORTED,
            base_observation,
            head_observation,
            None,
            "; ".join(sorted(set(unsupported))),
            durations,
        )
    if any(item["status"] in {"diverged", "protocol_error"} for item in observations):
        return ReplayResult(
            case.case_id,
            case.function_id,
            ReplayOutcome.DIVERGED,
            base_observation,
            head_observation,
            {"kind": "worker_divergence"},
            None,
            durations,
        )
    if any(item["status"] == "raised" for item in observations):
        return ReplayResult(
            case.case_id,
            case.function_id,
            ReplayOutcome.EXECUTION_ERROR,
            base_observation,
            head_observation,
            {"kind": "target_exception"},
            None,
            durations,
        )
    base_encoded = json.dumps(
        base_observation, sort_keys=True, allow_nan=False, separators=(",", ":")
    )
    head_encoded = json.dumps(
        head_observation, sort_keys=True, allow_nan=False, separators=(",", ":")
    )
    outcome = ReplayOutcome.IDENTICAL if base_encoded == head_encoded else ReplayOutcome.CHANGED
    divergence = None if outcome == ReplayOutcome.IDENTICAL else {"kind": "exact_json_changed"}
    return ReplayResult(
        case.case_id,
        case.function_id,
        outcome,
        base_observation,
        head_observation,
        divergence,
        None,
        durations,
    )


def replay(
    repo: Path,
    base: str,
    head: str,
    cases_path: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ReplayReport:
    if timeout_seconds < 1 or timeout_seconds > 60:
        raise BlastRadiusError("replay timeout must be between 1 and 60 seconds")
    base_identity = resolve_revision(repo, base)
    head_identity = resolve_revision(repo, head)
    cases = _load_cases(cases_path, base_identity)
    base_archive = _archive(repo, base_identity.sha)
    head_archive = _archive(repo, head_identity.sha)
    results: list[ReplayResult] = []
    with tempfile.TemporaryDirectory(prefix="blastradius-replay-") as temporary:
        temporary_root = Path(temporary)
        for index, case in enumerate(cases):
            case_root = temporary_root / f"case-{index}"
            base_root = case_root / "base"
            head_root = case_root / "head"
            base_home = case_root / "base-home"
            head_home = case_root / "head-home"
            for directory in (base_root, head_root, base_home, head_home):
                directory.mkdir(parents=True)
            _extract_archive(base_archive, base_root)
            _extract_archive(head_archive, head_root)
            base_observation, base_ms = _invoke(base_root, case, timeout_seconds, base_home)
            head_observation, head_ms = _invoke(head_root, case, timeout_seconds, head_home)
            results.append(
                _classify(
                    case,
                    base_observation,
                    head_observation,
                    {"base": round(base_ms, 2), "head": round(head_ms, 2)},
                )
            )
    return ReplayReport(
        SCHEMA_VERSION,
        ADAPTER_VERSION,
        base_identity,
        head_identity,
        base_identity,
        INPUT_SCHEMA,
        "exact strict JSON plus captured stdout/stderr; no fields are stripped",
        {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "isolation": (
                "separate sanitized subprocess and fresh filesystem snapshot per case and "
                "revision; not a security sandbox"
            ),
        },
        {
            "case_file_bytes": MAX_CASE_FILE_BYTES,
            "case_bytes": MAX_CASE_BYTES,
            "case_count": MAX_CASES,
            "worker_output_bytes": MAX_WORKER_OUTPUT_BYTES,
            "archive_bytes": MAX_ARCHIVE_BYTES,
            "archive_entries": MAX_ARCHIVE_ENTRIES,
            "timeout_seconds": timeout_seconds,
        },
        results,
        [
            "A changed output may be intended and is not automatically a regression.",
            (
                "Replay executes target code; process and filesystem isolation are not a "
                "security sandbox."
            ),
            (
                "Only explicit module-level synchronous functions with strict JSON "
                "inputs/outputs are supported."
            ),
        ],
    )


def write_replay_report(report: ReplayReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(to_primitive(report), indent=2, sort_keys=True) + "\n")
