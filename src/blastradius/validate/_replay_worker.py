"""Isolated JSON-call worker. Its stdout is reserved for one protocol response."""

from __future__ import annotations

import contextlib
import importlib
import inspect
import io
import json
import math
import sys
from pathlib import Path
from typing import Any

MAX_STREAM_BYTES = 16_384
MAX_PROTOCOL_BYTES = 1_048_576


class _BoundedText(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.bytes_written = 0
        self.exceeded = False

    def write(self, value: str) -> int:
        encoded_size = len(value.encode())
        if self.bytes_written + encoded_size > MAX_STREAM_BYTES:
            self.exceeded = True
            return len(value)
        self.bytes_written += encoded_size
        return super().write(value)


def _emit(payload: dict[str, Any]) -> None:
    stream = sys.__stdout__
    if stream is None:
        raise RuntimeError("worker protocol stream is unavailable")
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    if len(encoded.encode()) > MAX_PROTOCOL_BYTES:
        encoded = json.dumps(
            {"status": "unsupported", "reason": "serialized observation exceeds limit"},
            sort_keys=True,
        )
    stream.write(encoded + "\n")


def _import_target(root: Path, path: str) -> tuple[Path, str]:
    relative = Path(path)
    parts = list(relative.with_suffix("").parts)
    import_root = root
    if relative.parts[0] == "src" and len(relative.parts) > 1:
        import_root = root / "src"
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return import_root, ".".join(parts)


def _exception(exc: BaseException, stage: str, stdout: str, stderr: str) -> dict[str, Any]:
    return {
        "status": "raised",
        "stage": stage,
        "exception": {
            "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "message": str(exc)[:4096],
        },
        "stdout": stdout,
        "stderr": stderr,
    }


def _streams_supported(stdout: _BoundedText, stderr: _BoundedText) -> bool:
    return not stdout.exceeded and not stderr.exceeded


def _strict_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_strict_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _strict_json_value(item) for key, item in value.items())
    return False


def main() -> None:
    if len(sys.argv) != 4:
        _emit({"status": "protocol_error", "reason": "worker arguments are invalid"})
        return
    root = Path(sys.argv[1]).resolve()
    path = sys.argv[2]
    function_name = sys.argv[3]
    try:
        payload = json.load(sys.stdin)
        args = payload["args"]
        kwargs = payload["kwargs"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        _emit({"status": "protocol_error", "reason": f"invalid invocation payload: {exc}"})
        return

    stdout_buffer = _BoundedText()
    stderr_buffer = _BoundedText()
    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            import_root, module_name = _import_target(root, path)
            sys.path.insert(0, str(import_root))
            module = importlib.import_module(module_name)
            function = getattr(module, function_name)
    except BaseException as exc:
        stdout = stdout_buffer.getvalue()
        stderr = stderr_buffer.getvalue()
        if not _streams_supported(stdout_buffer, stderr_buffer):
            _emit({"status": "unsupported", "reason": "captured import output exceeds limit"})
        else:
            _emit(_exception(exc, "import", stdout, stderr))
        return

    if not inspect.isfunction(function) or function.__module__ != module.__name__:
        _emit(
            {
                "status": "unsupported",
                "reason": "adapter supports only Python functions defined at module scope",
            }
        )
        return
    if inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function):
        _emit(
            {
                "status": "unsupported",
                "reason": "async and generator functions require a dedicated adapter",
            }
        )
        return
    try:
        inspect.signature(function).bind(*args, **kwargs)
    except TypeError as exc:
        _emit({"status": "unsupported", "reason": f"inputs do not bind to function: {exc}"})
        return

    try:
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            value = function(*args, **kwargs)
    except BaseException as exc:
        stdout = stdout_buffer.getvalue()
        stderr = stderr_buffer.getvalue()
        if not _streams_supported(stdout_buffer, stderr_buffer):
            _emit({"status": "unsupported", "reason": "captured call output exceeds limit"})
        else:
            _emit(_exception(exc, "call", stdout, stderr))
        return

    stdout = stdout_buffer.getvalue()
    stderr = stderr_buffer.getvalue()
    if not _streams_supported(stdout_buffer, stderr_buffer):
        _emit({"status": "unsupported", "reason": "captured call output exceeds limit"})
        return
    observation = {
        "status": "returned",
        "value": value,
        "stdout": stdout,
        "stderr": stderr,
    }
    if not _strict_json_value(value):
        _emit({"status": "unsupported", "reason": "return value is not strict JSON"})
        return
    try:
        json.dumps(observation, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _emit({"status": "unsupported", "reason": f"return value is not strict JSON: {exc}"})
        return
    _emit(observation)


if __name__ == "__main__":
    main()
