"""Standalone, inert HTML reports with repository-controlled text escaped."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from blastradius.models import AnalysisReport, to_primitive


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def _items(values: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_text(value)}</li>" for value in values) + "</ul>"


def write_html(report: AnalysisReport, output: Path) -> None:
    """Render text as text, without scripts, remote assets, or repository-supplied URLs."""
    changed = []
    for change in report.changed_symbols:
        symbol = change.head or change.base
        assert symbol is not None
        lines = ", ".join(map(str, change.head_lines or change.base_lines)) or "structural"
        changed.append(
            "<tr>"
            + "".join(
                f"<td>{_text(value)}</td>"
                for value in (
                    change.change.value,
                    symbol.id.key,
                    lines,
                    f"{change.match_confidence:.2f}",
                    change.ambiguity or "",
                )
            )
            + "</tr>"
        )
    selected = [
        f"{test_id} — {'; '.join(report.selection.reasons.get(test_id, ['selected by policy']))}"
        for test_id in report.selection.selected_tests
    ]
    risk = "".join(
        "<tr>"
        + "".join(
            f"<td>{_text(value)}</td>"
            for value in (
                name,
                f"{report.risk.inputs[name]:.4f}",
                f"{weight:.1f}",
                f"{report.risk.contributions[name]:.2f}",
            )
        )
        + "</tr>"
        for name, weight in report.risk.weights.items()
    )
    overview = {
        "Repository": report.base.repository,
        "Base commit": report.base.sha,
        "Head commit": report.head.sha,
        "Mode": report.mode,
        "Risk": f"{report.risk.score:.2f}/100 — {report.risk.label}",
        "Selected known tests": (
            f"{len(report.selection.selected_tests)} / {len(report.selection.available_tests)}"
        ),
        "Fallback": report.selection.fallback.value,
        "Cache state": report.cache_state,
    }
    definitions = "".join(
        f"<dt>{_text(key)}</dt><dd>{_text(value)}</dd>" for key, value in overview.items()
    )
    document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>BlastRadius analysis</title>
<style>
body { font: 16px/1.5 system-ui, sans-serif; max-width: 76rem; margin: 2rem auto;
padding: 0 1rem; color: #18212b; background: #fff; overflow-wrap: anywhere; }
h1, h2 { line-height: 1.2; } h2 { margin-top: 2rem; }
table { border-collapse: collapse; width: 100%; } th, td { border: 1px solid #ccc;
padding: .5rem; text-align: left; vertical-align: top; }
dt { font-weight: bold; } dd { margin: 0 0 .6rem; }
pre { white-space: pre-wrap; background: #f3f5f7; padding: 1rem; }
</style></head><body><main><h1>BlastRadius analysis</h1>
"""
    document += (
        f"<dl>{definitions}</dl>"
        "<h2>Changed symbols</h2><table><thead><tr>"
        "<th>Change</th><th>Symbol</th><th>Lines</th><th>Match confidence</th>"
        "<th>Ambiguity</th></tr></thead><tbody>"
        + "".join(changed)
        + "</tbody></table><h2>Affected dependents</h2>"
        + _items(
            [
                f"{' → '.join(finding.dependency_path)} (confidence {finding.confidence:.2f})"
                for finding in report.impact
            ]
        )
        + "<h2>Selected tests</h2>"
        + _items(selected or ["No explicit subset is available. Follow the fallback policy."])
        + "<h2>Uncovered obligations</h2>"
        + _items(report.selection.uncovered_obligations)
        + "<h2>Truncated obligations</h2>"
        + _items(report.selection.truncated_obligations)
        + "<h2>Selection provenance</h2>"
        + _items(report.selection.provenance)
        + "<h2>Risk model</h2><p>"
        + _text(report.risk.model_version)
        + " — weighted sum clamped to 0-100.</p><table><thead><tr>"
        + "<th>Component</th><th>Input</th><th>Weight</th><th>Contribution</th>"
        + "</tr></thead><tbody>"
        + risk
        + "</tbody></table><h2>Warnings</h2>"
        + _items(report.warnings or ["None"])
        + "<h2>Interpretation</h2><p>Static edges are possible relationships; runtime edges "
        + "are observations from a declared workload. Coverage shows execution, not assertion "
        + "quality. The selected suite does not prove that the change is safe. Full-suite "
        + "fallback requires default test discovery, even when the known-test list is empty. "
        + "Risk is a heuristic prioritization score, not a failure probability.</p>"
        + "<details><summary>Complete analysis JSON</summary><pre>"
        + _text(json.dumps(to_primitive(report), indent=2, sort_keys=True))
        + "</pre></details></main></body></html>\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
