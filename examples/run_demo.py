"""Build a disposable repository and produce a real BlastRadius report."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip()


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "BlastRadius Demo",
            "GIT_AUTHOR_EMAIL": "demo@example.invalid",
            "GIT_COMMITTER_NAME": "BlastRadius Demo",
            "GIT_COMMITTER_EMAIL": "demo@example.invalid",
        }
    )
    return run(["git", *args], cwd=repo, env=env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("demo-output"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blastradius-demo-") as temp:
        repo = Path(temp)
        (repo / "tests").mkdir()
        (repo / "shop.py").write_text(
            "def round_price(value):\n"
            "    return round(value, 2)\n\n"
            "def checkout(value):\n"
            "    return round_price(value)\n\n"
            "def inventory():\n"
            "    return 10\n"
        )
        (repo / "tests" / "test_shop.py").write_text(
            "from shop import checkout, inventory\n\n"
            "def test_checkout():\n"
            "    assert checkout(1.236) == 1.24\n\n"
            "def test_inventory():\n"
            "    assert inventory() == 10\n"
        )
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD")
        replay_cases = output / "replay-cases.json"
        replay_cases.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "adapter_version": "json-call-v1",
                    "revision": {
                        "repository": repo.resolve().as_uri(),
                        "sha": base,
                        "tree": git(repo, "rev-parse", f"{base}^{{tree}}"),
                    },
                    "normalization": "exact",
                    "cases": [
                        {
                            "case_id": "checkout-rounding",
                            "function": {"path": "shop.py", "name": "checkout"},
                            "args": [1.236],
                            "kwargs": {},
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        profile = output / "base-coverage.json"
        run(
            [
                sys.executable,
                "-m",
                "blastradius.cli",
                "coverage",
                "collect",
                "--repo",
                str(repo),
                "--rev",
                base,
                "--target",
                "shop",
                "--profile-out",
                str(profile),
                "--",
                "tests",
            ],
            cwd=repo,
        )
        shop = repo / "shop.py"
        shop.write_text(shop.read_text().replace("round(value, 2)", "round(value, 1)"))
        git(repo, "add", "shop.py")
        git(repo, "commit", "-q", "-m", "change rounding policy")
        head = git(repo, "rev-parse", "HEAD")
        analysis_summary = run(
            [
                sys.executable,
                "-m",
                "blastradius.cli",
                "analyze",
                "--repo",
                str(repo),
                "--base",
                base,
                "--head",
                head,
                "--coverage",
                str(profile),
                "--mode",
                "conservative",
                "--json",
                str(output / "report.json"),
                "--markdown",
                str(output / "report.md"),
            ],
            cwd=repo,
        )
        replay_summary = run(
            [
                sys.executable,
                "-m",
                "blastradius.cli",
                "replay",
                "--repo",
                str(repo),
                "--base",
                base,
                "--head",
                head,
                "--cases",
                str(replay_cases),
                "--json",
                str(output / "replay.json"),
            ],
            cwd=repo,
        )
    print(analysis_summary)
    print(replay_summary)
    print(f"wrote demo artifacts to {output}")


if __name__ == "__main__":
    main()
