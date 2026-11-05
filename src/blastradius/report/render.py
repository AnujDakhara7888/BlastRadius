"""Stable JSON and explanatory Markdown rendering."""

from __future__ import annotations

import json
from pathlib import Path

from blastradius.models import AnalysisReport, to_primitive


def write_json(report: AnalysisReport, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(to_primitive(report), indent=2, sort_keys=True) + "\n")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|")


def write_markdown(report: AnalysisReport, output: Path) -> None:
    lines = [
        "# BlastRadius analysis",
        "",
        f"- Base: `{report.base.sha}`",
        f"- Head: `{report.head.sha}`",
        f"- Mode: `{report.mode}`",
        f"- Risk: **{report.risk.score:.2f}/100** ({report.risk.label})",
        f"- Selected tests: **{len(report.selection.selected_tests)} / "
        f"{len(report.selection.available_tests)}**",
        f"- Fallback: `{report.selection.fallback.value}`",
        "",
        "## Changed symbols",
        "",
    ]
    if report.changed_symbols:
        lines.extend(["| Change | Symbol | Lines |", "|---|---|---|"])
        for change in report.changed_symbols:
            symbol = change.head or change.base
            assert symbol is not None
            changed_lines = change.head_lines or change.base_lines
            lines.append(
                f"| {change.change.value} | `{_escape(symbol.id.key)}` | "
                f"{_escape(', '.join(map(str, changed_lines)) or 'structural')} |"
            )
    else:
        lines.append("No Python symbol changes were identified.")
    lines.extend(["", "## Affected dependents", ""])
    if report.impact:
        for finding in report.impact:
            path = " → ".join(f"`{_escape(item)}`" for item in finding.dependency_path)
            lines.append(f"- {path} (confidence {finding.confidence:.2f})")
    else:
        lines.append("No dependent symbols were found by the supported static analysis.")
    lines.extend(["", "## Selected tests", ""])
    if report.selection.selected_tests:
        for test_id in report.selection.selected_tests:
            reasons = "; ".join(report.selection.reasons.get(test_id, ["selected by policy"]))
            lines.append(f"- `{_escape(test_id)}` — {_escape(reasons)}")
    else:
        lines.append("No explicit subset is available. Follow the reported fallback policy.")
    lines.extend(
        [
            "",
            "## Risk model",
            "",
            "| Component | Input | Weight | Contribution |",
            "|---|---:|---:|---:|",
        ]
    )
    for name in report.risk.weights:
        lines.append(
            f"| {_escape(name)} | {report.risk.inputs[name]:.4f} | "
            f"{report.risk.weights[name]:.1f} | {report.risk.contributions[name]:.2f} |"
        )
    lines.extend(["", "The weighted sum is clamped to the range 0-100.", "", "## Warnings", ""])
    lines.extend(f"- {_escape(warning)}" for warning in report.warnings)
    if not report.warnings:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Static edges are possible relationships; runtime edges are observations from a "
            "declared workload. Coverage shows execution, not assertion quality. The selected "
            "suite is an informed subset and does not prove that the change is safe.",
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines))
