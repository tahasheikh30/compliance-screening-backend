"""
Generates the downloadable "proof" artifact for any screening HIT.

Every hit produces a one-page PDF evidence report containing:
  - Applicant details (name, CNIC, screening timestamp)
  - Which source matched (UNSC / FIA Red Book / Adverse Media)
  - The matched entry name + combined fuzzy-match confidence score, and
    (when available) the per-algorithm breakdown behind that number, so a
    reviewer isn't asked to trust a single opaque figure
  - Whether the match includes an exact CNIC match — flagged prominently,
    since that is direct identity evidence rather than a fuzzy inference
  - An embedded image where available:
      * UNSC: no live webpage to screenshot (it's a static XML feed), so
        the report includes the matched record fields as structured text
      * FIA Red Book: a rendered image of the actual PDF page containing
        the matched entry (see screening/fia_redbook.render_matched_page)
      * Adverse Media: a screenshot of the search/vendor results page

This PDF is what a compliance analyst attaches to the case file as
evidence for SECP/regulator review.
"""

from pathlib import Path
from datetime import datetime, timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from app.config import EVIDENCE_DIR


def _wrap_lines(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Word-wrap, and hard-break any single token (e.g. a long URL) wider than the page."""
    out: list[str] = []
    for line in simpleSplit(text, font, size, max_width) or [""]:
        while stringWidth(line, font, size) > max_width and len(line) > 1:
            cut = len(line)
            while cut > 1 and stringWidth(line[:cut], font, size) > max_width:
                cut -= 1
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out


def generate_evidence_pdf(
    applicant_name: str,
    cnic: str | None,
    source: str,
    matched_entry: str,
    score: float,
    source_url: str | None,
    image_path: Path | None,
    result_id: int,
    cnic_match: bool = False,
    breakdown: dict | None = None,
    list_version: str | None = None,
) -> Path:
    out_path = EVIDENCE_DIR / f"evidence_{result_id}.pdf"
    c = canvas.Canvas(str(out_path), pagesize=A4)
    width, height = A4
    margin = 20 * mm
    y = height - margin
    text_w = width - 2 * margin

    def wrapped(text, font="Helvetica", size=10, leading=5 * mm, color=None):
        """Draw text wrapped to the page width (long names/URLs used to run off the edge)."""
        nonlocal y
        c.setFont(font, size)
        if color:
            c.setFillColorRGB(*color)
        for line in _wrap_lines(str(text), font, size, text_w):
            c.drawString(margin, y, line)
            y -= leading
        if color:
            c.setFillColorRGB(0, 0, 0)

    # Header
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, y, "Screening Match Evidence Report")
    y -= 8 * mm
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(margin, y, f"Generated {datetime.now(timezone.utc).isoformat()}")
    c.setFillColorRGB(0, 0, 0)
    y -= 10 * mm

    c.line(margin, y, width - margin, y)
    y -= 10 * mm

    # CNIC exact match banner — most decisive evidence, shown first and
    # loudly, before the applicant/match detail blocks.
    if cnic_match:
        # Plain-text marker: "⚠" is not encodable in the built-in Helvetica
        # font and rendered as a stray "I" in the previous version.
        wrapped("ALERT - EXACT CNIC MATCH: direct identity evidence, not a fuzzy inference",
                font="Helvetica-Bold", size=12, leading=6 * mm, color=(0.7, 0, 0))
        y -= 4 * mm

    # Applicant block
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, "Applicant")
    y -= 6 * mm
    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"Name: {applicant_name}")
    y -= 6 * mm
    c.drawString(margin, y, f"CNIC: {cnic or 'Not provided'}")
    y -= 10 * mm

    # Match block
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, "Match Details")
    y -= 6 * mm
    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"Source: {source}")
    y -= 6 * mm
    wrapped(f"Matched entry: {matched_entry}", leading=5 * mm)
    y -= 1 * mm
    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"Combined fuzzy-match confidence: {score}/100")
    y -= 6 * mm
    if breakdown:
        parts = []
        for label, key in (("token-sort", "token_sort"), ("token-set", "token_set"),
                           ("partial", "partial"), ("weighted", "weighted")):
            if key in breakdown:
                parts.append(f"{label}={breakdown[key]:.1f}")
        if parts:
            c.setFont("Helvetica", 8)
            c.setFillColorRGB(0.35, 0.35, 0.35)
            c.drawString(margin, y, "Score breakdown: " + ", ".join(parts))
            c.setFillColorRGB(0, 0, 0)
            c.setFont("Helvetica", 10)
            y -= 6 * mm
    if list_version:
        wrapped(f"List version checked: {list_version}", size=9, leading=4.5 * mm)
        y -= 1.5 * mm
    if source_url:
        wrapped(f"Source reference: {source_url}", size=9, leading=4.5 * mm)
        y -= 1.5 * mm
    y -= 4 * mm

    wrapped(
        "This is an automated name/identity match, not a final adjudication. "
        "A compliance analyst must verify before any adverse action.",
        font="Helvetica-Oblique", size=8, leading=4 * mm, color=(0.5, 0.5, 0.5),
    )
    y -= 8 * mm

    # Embedded proof image
    if image_path and Path(image_path).exists():
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, "Source Record (captured image)")
        y -= 6 * mm
        try:
            img = ImageReader(str(image_path))
            iw, ih = img.getSize()
            max_w = width - 2 * margin
            max_h = max(y - margin, 20 * mm)
            scale = min(max_w / iw, max_h / ih)
            c.drawImage(img, margin, margin, width=iw * scale, height=ih * scale, preserveAspectRatio=True)
        except Exception as e:
            c.setFont("Helvetica", 9)
            c.drawString(margin, y, f"[Could not embed image: {e}]")
    else:
        c.setFont("Helvetica", 9)
        c.drawString(margin, y, "No captured image available for this source — see structured match details above.")

    c.showPage()
    c.save()
    return out_path
