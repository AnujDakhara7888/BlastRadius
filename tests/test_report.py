import json
from collections.abc import Callable
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

from test_analysis import BASE_SOURCE, make_change

from blastradius.impact.analyze import analyze
from blastradius.models import to_primitive
from blastradius.report.html import write_html
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


def test_html_keeps_hostile_report_text_inert_and_retains_complete_data(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    report = analyze(repo, base, head, None, None, "conservative")
    attack = '</pre><script>alert("x")</script><img src=x onerror=alert(1)>&`|'
    report.base = replace(report.base, repository=attack)
    report.mode = attack
    report.risk.label = attack
    report.warnings.append(attack)
    report.selection.selected_tests = [attack]
    report.selection.reasons[attack] = [attack]
    report.selection.provenance.append(attack)
    report.selection.uncovered_obligations.append(attack)
    report.selection.truncated_obligations.append(attack)
    change = report.changed_symbols[0]
    symbol = change.head or change.base
    assert symbol is not None
    report.changed_symbols[0] = replace(
        change, head=replace(symbol, id=replace(symbol.id, path=attack)), ambiguity=attack
    )
    if report.impact:
        report.impact[0] = replace(report.impact[0], dependency_path=(attack,))
    first, second = tmp_path / "first.html", tmp_path / "second.html"
    write_html(report, first)
    write_html(report, second)
    assert first.read_bytes() == second.read_bytes()

    class Inspect(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.tags: list[str] = []
            self.attributes: list[tuple[str, str | None]] = []
            self.text: list[str] = []
            self.pre: list[str] = []
            self.in_pre = False

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.tags.append(tag)
            self.attributes.extend(attrs)
            if tag == "pre":
                self.in_pre = True

        def handle_endtag(self, tag: str) -> None:
            if tag == "pre":
                self.in_pre = False

        def handle_data(self, data: str) -> None:
            self.text.append(data)
            if self.in_pre:
                self.pre.append(data)

    parsed = Inspect()
    parsed.feed(first.read_text())
    assert not {"script", "img", "iframe", "a", "form"} & set(parsed.tags)
    assert not any(
        name.startswith("on") or name in {"src", "href"} for name, _ in parsed.attributes
    )
    assert attack in "".join(parsed.text)
    assert json.loads("".join(parsed.pre)) == to_primitive(report)
    assert "Content-Security-Policy" in first.read_text()
    assert "not a failure probability" in first.read_text()
    assert "default test discovery" in first.read_text()
    markdown = tmp_path / "escaped.md"
    write_markdown(report, markdown)
    # Plain-text warnings escape HTML; literal identifiers use a longer code delimiter.
    assert "&lt;script&gt;" in markdown.read_text()
    assert f"`` {attack} ``" in markdown.read_text()


def test_analyze_cli_writes_optional_html(
    repo_factory: Callable[[dict[str, str]], Path], tmp_path: Path
) -> None:
    from blastradius.cli import build_parser, run

    repo = repo_factory({"pkg.py": BASE_SOURCE})
    base, head = make_change(repo)
    html = tmp_path / "analysis.html"
    args = build_parser().parse_args(
        [
            "analyze",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--json",
            str(tmp_path / "report.json"),
            "--markdown",
            str(tmp_path / "report.md"),
            "--html",
            str(html),
            "--no-cache",
        ]
    )
    assert run(args) == 0
    assert head in html.read_text()
    assert "full_suite" in html.read_text()
