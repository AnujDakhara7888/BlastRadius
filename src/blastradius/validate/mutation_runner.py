"""Internal matched selected/full pytest experiments; no mutation-score claims."""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath

from blastradius.errors import BlastRadiusError
from blastradius.models import (
    SCHEMA_VERSION,
    MutantExecutionResult,
    MutantGenerationReport,
    MutantSpec,
    MutationExecutionReport,
    MutationOutcome,
    MutationSuiteResult,
    SuiteExecution,
    SuiteStatus,
    to_primitive,
)
from blastradius.validate.mutate import GENERATOR_VERSION, apply_mutant
from blastradius.validate.replay import _archive, _extract_archive
from blastradius.vcs.git import resolve_revision

RUNNER_VERSION = "isolated-pytest-v1"
MAX_OUTPUT_BYTES = 1_048_576


def _selectors(values: Sequence[str]) -> list[str]:
    for value in values:
        source_path = value.split("::", 1)[0]
        relative = PurePosixPath(source_path)
        if (
            not source_path
            or source_path.startswith("-")
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in source_path
            or "\x00" in value
        ):
            raise BlastRadiusError(f"suite selectors must be repository-relative paths: {value!r}")
    return sorted(set(values))


def _environment(home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"}
    }
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            "TEMP": str(home),
            "TMP": str(home),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "BLASTRADIUS_MUTATION": "1",
        }
    )
    return environment


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif process.poll() is None:
        process.kill()
    process.wait()


def _observe(
    directory: Path, root: Path, selectors: list[str], timeout_seconds: int
) -> SuiteExecution:
    home = directory / "home"
    home.mkdir()
    observation = directory / "observation.json"
    stdout_path = directory / "stdout.txt"
    stderr_path = directory / "stderr.txt"
    started = time.perf_counter()
    status: SuiteStatus | None = None
    reason = ""
    return_code: int | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("_mutation_worker.py")),
                    str(root),
                    str(observation),
                    json.dumps(selectors),
                ],
                cwd=root,
                env=_environment(home),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=os.name == "posix",
            )
            try:
                while True:
                    if stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_OUTPUT_BYTES:
                        status, reason = SuiteStatus.OUTPUT_LIMIT, "worker output exceeded limit"
                        break
                    remaining = timeout_seconds - (time.perf_counter() - started)
                    if remaining <= 0:
                        status, reason = SuiteStatus.TIMEOUT, "worker timed out"
                        break
                    try:
                        return_code = process.wait(timeout=min(0.05, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                # Also clean up descendants left behind by a completed pytest worker on POSIX.
                _stop_process_group(process)
    except OSError as exc:
        status, reason = SuiteStatus.INFRASTRUCTURE_ERROR, f"worker launch/I/O failed: {exc}"
    duration = round((time.perf_counter() - started) * 1000, 2)
    stdout_text = stderr_text = ""
    try:
        if stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_OUTPUT_BYTES:
            status, reason = SuiteStatus.OUTPUT_LIMIT, "worker output exceeded limit"
        with stdout_path.open("rb") as stream:
            stdout_bytes = stream.read(MAX_OUTPUT_BYTES)
        with stderr_path.open("rb") as stream:
            stderr_bytes = stream.read(MAX_OUTPUT_BYTES - len(stdout_bytes))
        stdout_text = stdout_bytes.decode(errors="replace")
        stderr_text = stderr_bytes.decode(errors="replace")
    except OSError as exc:
        status, reason = SuiteStatus.INFRASTRUCTURE_ERROR, f"cannot read worker logs: {exc}"
    counts = {"collected": 0, "passed": 0, "failed": 0, "skipped": 0}
    identities: dict[str, list[str]] = {"collected_tests": [], "passed_tests": []}
    if status is None:
        try:
            if return_code != 0:
                raise ValueError(f"worker exited abnormally with status {return_code}")
            if observation.stat().st_size > MAX_OUTPUT_BYTES:
                raise ValueError("worker observation exceeded limit")
            raw = json.loads(observation.read_text())
            for key in (*counts, "exit_code", "collection_errors"):
                if type(raw[key]) is not int or raw[key] < 0:
                    raise ValueError(f"invalid worker count {key}")
            counts = {key: raw[key] for key in counts}
            for key in identities:
                items = raw[key]
                if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                    raise ValueError(f"invalid worker test identities {key}")
                identities[key] = items
            if (
                len(identities["collected_tests"]) != counts["collected"]
                or len(identities["passed_tests"]) != counts["passed"]
                or not set(identities["passed_tests"]) <= set(identities["collected_tests"])
                or sum(counts[key] for key in ("passed", "failed", "skipped")) > counts["collected"]
            ):
                raise ValueError("inconsistent worker counts or test identities")
            return_code = raw["exit_code"]
            if raw["collection_errors"] or return_code not in {0, 1, 5}:
                status, reason = (
                    SuiteStatus.INFRASTRUCTURE_ERROR,
                    "pytest collection/configuration error",
                )
            elif return_code == 1 and counts["failed"]:
                status, reason = SuiteStatus.TEST_FAILURE, "pytest reported failing test phases"
            elif return_code == 0 and counts["passed"] and not counts["failed"]:
                status, reason = SuiteStatus.PASSED, "pytest completed with passing test calls"
            else:
                status, reason = SuiteStatus.NO_TESTS, "no passing or failing test calls observed"
        except (OSError, ValueError, TypeError, KeyError) as exc:
            status, reason = SuiteStatus.INFRASTRUCTURE_ERROR, f"invalid worker observation: {exc}"
    return SuiteExecution(
        status,
        return_code,
        collected=counts["collected"],
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        duration_ms=duration,
        reason=reason,
        stdout=stdout_text,
        stderr=stderr_text,
        collected_tests=identities["collected_tests"],
        passed_tests=identities["passed_tests"],
    )


def _execute(
    archive: bytes,
    directory: Path,
    selectors: list[str],
    timeout_seconds: int,
    mutant: MutantSpec | None = None,
) -> SuiteExecution:
    root = directory / "snapshot"
    root.mkdir(parents=True)
    _extract_archive(archive, root)
    if mutant is not None:
        source_path = root / mutant.path
        source_path.write_bytes(apply_mutant(source_path.read_bytes().decode(), mutant).encode())
    return _observe(directory, root, selectors, timeout_seconds)


def _result(execution: SuiteExecution, *, baseline: bool = False) -> MutationSuiteResult:
    if execution.status == SuiteStatus.PASSED:
        outcome = MutationOutcome.SURVIVED
    elif execution.status == SuiteStatus.TEST_FAILURE:
        outcome = MutationOutcome.BASELINE_FAILED if baseline else MutationOutcome.KILLED
    elif execution.status == SuiteStatus.INFRASTRUCTURE_ERROR:
        outcome = MutationOutcome.INFRASTRUCTURE_ERROR
    else:
        outcome = MutationOutcome.INCONCLUSIVE
    reason = f"clean-head baseline: {execution.reason}" if baseline else execution.reason
    return MutationSuiteResult(outcome, reason, None if baseline else execution)


def run_mutants(
    repo: Path,
    generation: MutantGenerationReport,
    selected_tests: Sequence[str],
    *,
    full_suite: Sequence[str] = (),
    timeout_seconds: int = 30,
) -> MutationExecutionReport:
    """Run each suite only after its own unmodified head baseline passes.

    Uses the current interpreter and installed dependencies. Only trusted target code is supported;
    fresh snapshots and sanitized subprocesses do not constitute a security sandbox.
    """
    if not 1 <= timeout_seconds <= 1800:
        raise BlastRadiusError("mutation timeout must be between 1 and 1800 seconds")
    selected = _selectors(selected_tests)
    full = _selectors(full_suite)
    if (
        generation.schema_version != SCHEMA_VERSION
        or generation.generator_version != GENERATOR_VERSION
    ):
        raise BlastRadiusError("unsupported mutation generation schema or version")
    if (
        resolve_revision(repo, generation.head.sha) != generation.head
        or resolve_revision(repo, generation.base.sha) != generation.base
    ):
        raise BlastRadiusError("mutation generation identity does not match this repository")
    seen: set[str] = set()
    for mutant in generation.mutants:
        relative = PurePosixPath(mutant.path)
        if (
            mutant.revision != generation.head.sha
            or mutant.validity != "valid"
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in mutant.path
            or "\x00" in mutant.path
            or relative.suffix != ".py"
            or mutant.mutant_id in seen
        ):
            raise BlastRadiusError("invalid, duplicate, or stale mutant specification")
        seen.add(mutant.mutant_id)
    archive = _archive(repo, generation.head.sha)
    baselines: dict[str, SuiteExecution] = {}
    results: list[MutantExecutionResult] = []
    with tempfile.TemporaryDirectory(prefix="blastradius-mutation-") as temporary:
        temporary_root = Path(temporary)
        validation_root = temporary_root / "validation"
        validation_root.mkdir()
        _extract_archive(archive, validation_root)
        # Validate exact archived bytes before any target code is allowed to execute.
        for mutant in generation.mutants:
            try:
                source = (validation_root / mutant.path).read_bytes().decode()
                compile(apply_mutant(source, mutant), mutant.path, "exec")
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                raise BlastRadiusError(
                    f"cannot materialize mutant {mutant.mutant_id}: {exc}"
                ) from exc
        suites = {"selected": selected, "full": full}
        for suite, selectors in suites.items():
            if suite == "selected" and not selectors:
                baselines[suite] = SuiteExecution(
                    SuiteStatus.NO_TESTS, None, 0, 0, 0, 0, 0, "selected suite is empty"
                )
            else:
                baselines[suite] = _execute(
                    archive, temporary_root / f"baseline-{suite}", selectors, timeout_seconds
                )
        for index, mutant in enumerate(generation.mutants):
            outcomes: dict[str, MutationSuiteResult] = {}
            for suite, selectors in suites.items():
                baseline = baselines[suite]
                if baseline.status != SuiteStatus.PASSED:
                    outcomes[suite] = _result(baseline, baseline=True)
                else:
                    execution = _execute(
                        archive,
                        temporary_root / f"mutant-{index}-{suite}",
                        selectors,
                        timeout_seconds,
                        mutant,
                    )
                    if execution.status == SuiteStatus.PASSED and (
                        execution.collected_tests != baseline.collected_tests
                        or execution.passed_tests != baseline.passed_tests
                    ):
                        outcomes[suite] = MutationSuiteResult(
                            MutationOutcome.INCONCLUSIVE,
                            "passing run exercised different tests from its clean-head baseline",
                            execution,
                        )
                    else:
                        outcomes[suite] = _result(execution)
            results.append(MutantExecutionResult(mutant, outcomes["selected"], outcomes["full"]))
    return MutationExecutionReport(
        SCHEMA_VERSION,
        RUNNER_VERSION,
        generation,
        selected,
        full,
        baselines,
        results,
        {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "dependencies": "current interpreter; pytest plugin autoload disabled",
            "isolation": (
                "fresh head snapshot and sanitized subprocess per run; not a security sandbox"
            ),
            "descendant_cleanup": "process group" if os.name == "posix" else "direct worker only",
        },
        {"timeout_seconds": timeout_seconds, "output_bytes": MAX_OUTPUT_BYTES},
        [
            "Clean-head baselines and both mutant suites use matched timeout/environment policies.",
            "Timeouts, skipped-only suites and infrastructure errors are not kills or survivals.",
            "A surviving mutant is not evidence of semantic equivalence.",
            "Target code executes locally; only trusted, side-effect-controlled code is supported.",
            "These internal execution observations do not yet define mutation-score denominators.",
        ],
    )


def write_mutation_execution_report(report: MutationExecutionReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(to_primitive(report), indent=2, sort_keys=True) + "\n")
