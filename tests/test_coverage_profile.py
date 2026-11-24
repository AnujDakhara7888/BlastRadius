from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from blastradius.coverage.profile import collect_profile


@pytest.mark.parametrize(
    ("config_name", "config_text"),
    [
        (None, ""),
        ("pytest.ini", "[pytest]\npython_files = check_*.py\n"),
        ("pyproject.toml", '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n'),
        ("tox.ini", "[pytest]\npython_files = check_*.py\n"),
        ("setup.cfg", "[tool:pytest]\npython_files = check_*.py\n"),
        # An unrelated packaging file must not hide the actual pytest config.
        ("setup.cfg", "[metadata]\nname = example\n"),
    ],
)
def test_nested_collection_uses_target_config_and_relative_node_ids(
    tmp_path: Path,
    repo_factory: Callable[[dict[str, str]], Path],
    monkeypatch: pytest.MonkeyPatch,
    config_name: str | None,
    config_text: str,
) -> None:
    custom_discovery = config_name is not None and "python_files" in config_text
    filename = "check_logic.py" if custom_discovery else "test_logic.py"
    files = {
        "logic.py": "def answer():\n    return 42\n",
        f"tests/{filename}": (
            "from logic import answer\ndef test_answer():\n    assert answer() == 42\n"
        ),
    }
    if config_name:
        files[config_name] = config_text
    repo = repo_factory(files)
    # Both configuration and conftest discovery must stop at the repository.
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --invalid-ancestor-option\n")
    (tmp_path / "conftest.py").write_text("raise RuntimeError('ancestor conftest imported')\n")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    profile = collect_profile(
        repo, "HEAD", "logic", tmp_path / "profile.json", ["-p", "pytest_cov.plugin"]
    )
    assert list(profile.test_lines) == [f"tests/{filename}::test_answer"]
    assert profile.test_lines[f"tests/{filename}::test_answer"]["logic.py"] == [2]
    assert not (repo / "pytest.ini").exists() or config_name == "pytest.ini"
