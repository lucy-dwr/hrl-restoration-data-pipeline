from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

from .models import Finding, Report


def _label(value: str) -> str:
    """Turn a machine-readable identifier into a compact report label."""
    return value.replace("_", " ").title()


_RULE_LABELS = {
    "stage_requiredness": "Required fields for the project stage",
}


def _rule_label(rule: str) -> str:
    """Give operators a human-readable name for a validation check."""
    return _RULE_LABELS.get(rule, _label(rule))


def _readable_message(message: str) -> str:
    """Replace schema-style field names with labels in the human reports."""
    return re.sub(r"\b[a-z][a-z0-9_]*_[a-z0-9_]+\b", lambda match: _label(match.group()), message)


def _metadata_rows(values: dict[str, str]) -> str:
    return "".join(
        f"<tr><th>{escape(_label(key))}</th><td>{escape(str(value))}</td></tr>"
        for key, value in values.items()
    )


def _finding_rows(report: Report, findings: list[Finding]) -> str:
    rows = []
    for finding in findings:
        record_id = escape(finding.record_id or "—")
        record_name = report.record_names.get(finding.record_id or "")
        record = record_id if not record_name else f"<strong>{record_id}</strong><br><span>{escape(record_name)}</span>"
        rows.append(
            "<tr>"
            f"<td>{escape(_rule_label(finding.rule))}<br><span class='stage'>{escape(_label(finding.stage))}</span></td>"
            f"<td>{record}</td>"
            f"<td>{escape(_readable_message(finding.message))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _finding_table(report: Report, heading: str, findings: list[Finding]) -> str:
    if not findings:
        return ""
    return (
        f"<h3>{heading} ({len(findings)})</h3>"
        "<table class='findings'><thead><tr><th>Check</th><th>Record</th>"
        "<th>What needs review</th></tr></thead>"
        f"<tbody>{_finding_rows(report, findings)}</tbody></table>"
    )


def _pdf_finding_table(report: Report, findings: list[Finding], severity: str):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, Table, TableStyle

    styles = getSampleStyleSheet()
    cell = styles["BodyText"].clone(f"{severity.lower()}-cell")
    cell.fontSize = 8.5
    cell.leading = 10.5
    cell.wordWrap = "LTR"
    header = styles["BodyText"].clone("finding-header")
    header.textColor = colors.white
    header.fontName = "Helvetica-Bold"
    severity_color = colors.HexColor("#9b1c1c") if severity == "ERROR" else colors.HexColor("#8a4b00")
    rows = [[
        Paragraph("Check", header),
        Paragraph("Record", header),
        Paragraph("What needs review", header),
    ]]
    for finding in findings:
        record_id = escape(finding.record_id or "—")
        record_name = report.record_names.get(finding.record_id or "")
        record = record_id if not record_name else f"<b>{record_id}</b><br/>{escape(record_name)}"
        rows.append([
            Paragraph(f"<b>{escape(_rule_label(finding.rule))}</b><br/>{escape(_label(finding.stage))}", cell),
            Paragraph(record, cell),
            Paragraph(escape(_readable_message(finding.message)), cell),
        ])
    table = Table(rows, colWidths=(128, 145, 195), repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), severity_color),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#aaaaaa")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#fff8e8") if severity == "WARNING" else colors.HexColor("#fff0f0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _write_pdf(report: Report, path: Path) -> None:
    """Render an offline, deterministic human-readable companion report."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    story = [
        Paragraph("HRL Validation Report", styles["Title"]),
        Paragraph(report.status, styles["Heading1"]),
        Paragraph(f"Validated: {escape(report.validation_timestamp)}", styles["BodyText"]),
        Paragraph(f"Errors: {len(report.errors)}; Warnings: {len(report.warnings)}", styles["BodyText"]),
    ]
    if report.warnings:
        warning = styles["Heading2"].clone("warning")
        warning.textColor = colors.darkorange
        story.append(Paragraph(f"PASSED WITH {len(report.warnings)} WARNING(S): REVIEW BEFORE APPROVAL", warning))
    story.append(Spacer(1, 12))
    for heading, values in (("Submission metadata", report.submission_metadata), ("Input file", report.input_file)):
        if values:
            story.append(Paragraph(heading, styles["Heading2"]))
            table = Table([[_label(key), str(value)] for key, value in values.items()], colWidths=(150, 360))
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story.extend((table, Spacer(1, 12)))
    story.append(Paragraph("Findings", styles["Heading2"]))
    for heading, findings, severity in (("Errors", report.errors, "ERROR"), ("Warnings", report.warnings, "WARNING")):
        if findings:
            story.append(Paragraph(f"{heading} ({len(findings)})", styles["Heading3"]))
            story.extend((_pdf_finding_table(report, findings, severity), Spacer(1, 12)))
    if not report.findings:
        story.append(Paragraph("No findings.", styles["BodyText"]))

    from reportlab.pdfgen.canvas import Canvas

    def deterministic_canvas(filename, **kwargs):
        kwargs["invariant"] = 1
        return Canvas(filename, **kwargs)

    SimpleDocTemplate(str(path), pagesize=letter, title="HRL validation report").build(story, canvasmaker=deterministic_canvas)


def write_reports(report: Report, directory: Path, *, pdf: bool = False) -> None:
    """Write JSON and accessible HTML; write a PDF companion when requested."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath("validation-report.json").write_text(json.dumps(report.json(), indent=2, sort_keys=True), encoding="utf-8")
    warnings = "" if not report.warnings else f"<div class='warning-banner'>PASSED WITH {len(report.warnings)} WARNING(S): REVIEW BEFORE APPROVAL</div>"
    findings = _finding_table(report, "Errors", report.errors) + _finding_table(report, "Warnings", report.warnings)
    if not findings:
        findings = "<p>No findings.</p>"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>HRL validation report</title>
<style>
body{{font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:2rem;color:#242424}}
.status{{font-size:1.5rem;font-weight:700}} .warning-banner{{color:#6b3a00;background:#fff3cd;padding:.8rem;font-weight:700}}
table{{border-collapse:collapse;width:100%;margin:0 0 1.5rem;table-layout:fixed}} th,td{{border:1px solid #bbb;padding:.55rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}} th{{background:#eee}}
.findings th{{background:#343a40;color:#fff}} .findings th:nth-child(1){{width:25%}} .findings th:nth-child(2){{width:30%}} .findings th:nth-child(3){{width:45%}} .findings td{{background:#fffdf6}}
.stage{{color:#555;font-size:.9em}} @media print{{body{{margin:.5in}}}}
</style></head><body><h1>HRL Validation Report</h1><p class="status">{escape(report.status)}</p>
<p>Validated: {escape(report.validation_timestamp)}</p>{warnings}<p>Errors: {len(report.errors)}; Warnings: {len(report.warnings)}</p>
<h2>Submission metadata</h2><table>{_metadata_rows(report.submission_metadata)}</table>
<h2>Input file</h2><table>{_metadata_rows(report.input_file)}</table><h2>Findings</h2>{findings}</body></html>"""
    directory.joinpath("validation-report.html").write_text(html, encoding="utf-8")
    if pdf:
        _write_pdf(report, directory / "validation-report.pdf")
