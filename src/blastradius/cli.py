"""BlastRadius command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from blastradius import __version__
from blastradius.coverage.profile import collect_profile
from blastradius.errors import BlastRadiusError
from blastradius.impact.analyze import analyze
from blastradius.report.render import write_json, write_markdown
from blastradius.runtime.profile import collect_runtime_profile


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blastradius",
        description="Explain which tests and dependents a Python change may affect.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    analyze_parser = commands.add_parser("analyze", help="analyze a base/head revision range")
    analyze_parser.add_argument("--repo", type=_path, required=True)
    analyze_parser.add_argument("--base", required=True)
    analyze_parser.add_argument("--head", required=True)
    analyze_parser.add_argument("--coverage", type=_path)
    analyze_parser.add_argument("--runtime", type=_path)
    analyze_parser.add_argument(
        "--mode", choices=("conservative", "balanced", "aggressive"), default="conservative"
    )
    analyze_parser.add_argument("--json", type=_path, required=True)
    analyze_parser.add_argument("--markdown", type=_path, required=True)
    analyze_parser.add_argument(
        "--cache-file",
        type=_path,
        help="override the Git-owned SQLite cache path",
    )
    analyze_parser.add_argument(
        "--no-cache", action="store_true", help="disable persistent static-index caching"
    )

    coverage_parser = commands.add_parser("coverage", help="per-test coverage profiles")
    coverage_commands = coverage_parser.add_subparsers(dest="coverage_command", required=True)
    collect = coverage_commands.add_parser("collect", help="run pytest-cov with per-test contexts")
    collect.add_argument("--repo", type=_path, required=True)
    collect.add_argument("--rev", required=True)
    collect.add_argument("--target", required=True, help="package or source path passed to --cov")
    collect.add_argument("--profile-out", type=_path, required=True)
    collect.add_argument("pytest_args", nargs=argparse.REMAINDER)

    trace = commands.add_parser("trace", help="trace an explicit representative workload")
    trace.add_argument("--repo", type=_path, required=True)
    trace.add_argument("--rev", required=True)
    trace.add_argument("--workload", type=Path, required=True)
    trace.add_argument("--profile-out", type=_path, required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.command == "coverage" and args.coverage_command == "collect":
        pytest_args = args.pytest_args
        if pytest_args[:1] == ["--"]:
            pytest_args = pytest_args[1:]
        collect_profile(args.repo, args.rev, args.target, args.profile_out, pytest_args)
        print(f"wrote coverage profile: {args.profile_out}")
        return 0
    if args.command == "trace":
        collect_runtime_profile(args.repo, args.rev, args.workload, args.profile_out)
        print(f"wrote representative-workload profile: {args.profile_out}")
        return 0
    if args.command == "analyze":
        report = analyze(
            args.repo,
            args.base,
            args.head,
            args.coverage,
            args.runtime,
            args.mode,
            use_cache=not args.no_cache,
            cache_file=args.cache_file,
        )
        write_json(report, args.json)
        write_markdown(report, args.markdown)
        print(
            f"selected {len(report.selection.selected_tests)}/"
            f"{len(report.selection.available_tests)} known tests; "
            f"risk={report.risk.score:.2f}/100; "
            f"fallback={report.selection.fallback.value}"
        )
        return 0
    raise BlastRadiusError("no command selected")


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except BlastRadiusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
