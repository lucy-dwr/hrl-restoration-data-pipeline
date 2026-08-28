from __future__ import annotations
import json
from html import escape
from pathlib import Path
from .models import Report


def _write_pdf(report: Report, path: Path) -> None:
    """Render an offline, deterministic human-readable companion report."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    story = [Paragraph("HRL Validation Report", styles["Title"]),
             Paragraph(report.status, styles["Heading1"]),
             Paragraph(f"Validated: {escape(report.validation_timestamp)}", styles["BodyText"]),
             Paragraph(f"Errors: {len(report.errors)}; Warnings: {len(report.warnings)}", styles["BodyText"])]
    if report.warnings:
        warning = styles["Heading2"].clone("warning"); warning.textColor = colors.darkorange
        story.append(Paragraph(f"PASSED WITH {len(report.warnings)} WARNING(S): REVIEW BEFORE APPROVAL", warning))
    story.append(Spacer(1, 12))
    for finding in report.findings:
        style = styles["BodyText"].clone(f"finding-{len(story)}")
        style.textColor = colors.darkred if finding.severity == "ERROR" else colors.darkorange if finding.severity == "WARNING" else colors.black
        story.append(Paragraph(f"<b>{escape(finding.severity)}</b> [{escape(finding.stage)}] {escape(finding.message)}", style))
    from reportlab.pdfgen.canvas import Canvas
    def deterministic_canvas(filename, **kwargs):
        kwargs["invariant"] = 1
        return Canvas(filename, **kwargs)
    SimpleDocTemplate(str(path), pagesize=letter, title="HRL validation report").build(story, canvasmaker=deterministic_canvas)

def write_reports(report: Report, directory: Path, *, pdf: bool = False) -> None:
    """Write the JSON report and its HTML companion; add the PDF only when asked.

    JSON is the authoritative report. HTML is the copy an operator forwards to a
    provider. PDF is opt-in (``--pdf``) and needs the ``pdf`` extra installed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    directory.joinpath("validation-report.json").write_text(json.dumps(report.json(), indent=2, sort_keys=True))
    findings = "".join(f"<li class='{f.severity.lower()}'><b>{escape(f.severity)}</b> [{escape(f.stage)}] {escape(f.message)}</li>" for f in report.findings)
    warnings = "" if not report.warnings else f"<div class='warning-banner'>PASSED WITH {len(report.warnings)} WARNING(S): REVIEW BEFORE APPROVAL</div>"
    html = f"""<!doctype html><title>HRL validation report</title><style>body{{font:16px sans-serif;margin:2rem}}.warning-banner,.warning{{color:#7a4300;background:#fff3cd;padding:.7rem}}.error{{color:#9b1c1c}}.status{{font-size:1.5rem;font-weight:bold}}</style><h1>HRL Validation Report</h1><p class='status'>{report.status}</p><p>Validated: {escape(report.validation_timestamp)}</p>{warnings}<p>Errors: {len(report.errors)}; Warnings: {len(report.warnings)}</p><ul>{findings}</ul></html>"""
    directory.joinpath("validation-report.html").write_text(html)
    if pdf:
        _write_pdf(report, directory / "validation-report.pdf")
