from collections.abc import Callable
from pathlib import Path

from test_analysis import BASE_SOURCE, make_change

from blastradius.impact.analyze import analyze
from blastradius.report.render import write_json, write_markdown


def test_reports_are_deterministic_and_explain_limitations(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    report = analyze(repo, base, head, None, None, "conservative")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    markdown = tmp_path / "report.md"
    write_json(report, first)
    write_json(report, second)
    write_markdown(report, markdown)
    assert first.read_text() == second.read_text()
    assert "not a failure probability" in markdown.read_text()
    assert "does not prove" in markdown.read_text()
