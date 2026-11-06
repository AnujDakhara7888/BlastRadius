"""Private pytest worker, launched with -I in a fresh immutable-head snapshot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


class _Observations:
    def __init__(self) -> None:
        self.collected = 0
        self.collected_tests: list[str] = []
        self.passed: set[str] = set()
        self.failed: set[str] = set()
        self.skipped: set[str] = set()
        self.collection_errors = 0

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = len(session.items)
        self.collected_tests = sorted(item.nodeid for item in session.items)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_errors += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.skipped:
            self.skipped.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)


def main() -> None:
    root = Path(sys.argv[1])
    output = Path(sys.argv[2])
    selectors: list[str] = json.loads(sys.argv[3])
    # Prefer this snapshot over an installed copy of the target package.
    sys.path[:0] = [str(root / "src"), str(root)]
    observations = _Observations()
    exit_code = int(
        pytest.main(
            ["-q", "--capture=no", "-p", "no:cacheprovider", "--", *selectors],
            plugins=[observations],
        )
    )
    output.write_text(
        json.dumps(
            {
                "exit_code": exit_code,
                "collected": observations.collected,
                "passed": len(observations.passed - observations.failed - observations.skipped),
                "failed": len(observations.failed),
                "skipped": len(observations.skipped - observations.failed),
                "collection_errors": observations.collection_errors,
                "collected_tests": observations.collected_tests,
                "passed_tests": sorted(
                    observations.passed - observations.failed - observations.skipped
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
