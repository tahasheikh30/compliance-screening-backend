"""
Evidence PDF for a screening that found a potential match.

Port of the "Build Evidence PDF" node of the n8n workflow (same sections, same
order and the same wording where it matters), drawn with reportlab instead of
hand written PDF objects:

    header banner, result banner, summary cards, applicant, screening details,
    lists screened (with per list result), method note, watch-list matches
    (one card each), adverse media articles, reviewer decision block, footer.

One PDF is produced per screening, not per source.
"""

import re
import unicodedata
from pathlib import Path

from reportlab.lib.colors import Color
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from app.config import EVIDENCE_DIR

PAGE_W, PAGE_H = 595, 842
LEFT, RIGHT, CW = 50, 545, 495
TOP, BOTTOM = 790, 62
PAD_R = 16

F1, F2, F3 = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


def _c(r, g, b):
    return Color(r, g, b)


COL = {
    "navy": _c(0.16, 0.22, 0.30), "blue": _c(0.24, 0.42, 0.56), "ink": _c(0.15, 0.17, 0.20),
    "muted": _c(0.44, 0.47, 0.51), "line": _c(0.87, 0.88, 0.89), "band": _c(0.96, 0.96, 0.95),
    "white": _c(1, 1, 1), "red": _c(0.66, 0.25, 0.23), "amber": _c(0.78, 0.57, 0.20),
    "green": _c(0.29, 0.53, 0.42), "amber_bg": _c(0.98, 0.95, 0.89), "soft": _c(0.72, 0.77, 0.82),
}


def clean(s) -> str:
    """Standard PDF fonts only cover basic Latin: fold accents, replace the rest with '?'."""
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[\u0300-\u036f]", "", s)
    s = re.sub(r"[\u2018\u2019]", "'", s)
    s = re.sub(r"[\u201c\u201d]", '"', s)
    s = re.sub(r"[\u2013\u2014]", "-", s)
    return re.sub(r"[^\x20-\x7e]", "?", s)


def fmt(v) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return "-" if v is None or v == "" else str(v)


def fmt_date(s) -> str:
    t = fmt(s)
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", t):
        return t[:10] + " " + t[11:19] + " UTC"
    return t


def num(n) -> str:
    return f"{int(n or 0):,}"


def tw(s, size, bold=False) -> float:
    return stringWidth(clean(s), F2 if bold else F1, size)


def wrap(text, max_w, size, bold=False) -> list:
    words = clean(text).split()
    lines, cur = [], ""
    for w in words:
        while tw(w, size, bold) > max_w:
            if cur:
                lines.append(cur)
                cur = ""
            k = 1
            while k < len(w) and tw(w[: k + 1], size, bold) <= max_w:
                k += 1
            lines.append(w[:k])
            w = w[k:]
        t = (cur + " " if cur else "") + w
        if cur and tw(t, size, bold) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines or [""]


def fit_text(s, size, bold, max_w) -> str:
    s = clean(s)
    if tw(s, size, bold) <= max_w:
        return s
    while len(s) > 1 and tw(s + "...", size, bold) > max_w:
        s = s[:-1]
    return s + "..."


class _NumberedCanvas(canvas.Canvas):
    """Canvas that knows the total page count, so the footer can say 'Page X of Y'."""

    footer_name = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total):
        self.setStrokeColor(COL["line"])
        self.setLineWidth(0.6)
        self.line(LEFT, 46, RIGHT, 46)
        self.setFillColor(COL["muted"])
        self.setFont(F1, 8)
        self.drawString(LEFT, 32, fit_text("Sanctions screening evidence | " + self.footer_name, 8, False, 380))
        pn = f"Page {self._pageNumber} of {total}"
        self.drawString(RIGHT - tw(pn, 8), 32, pn)


class _Doc:
    """A tiny top-down layout helper. y is the current cursor, measured from the page bottom."""

    def __init__(self, path: Path, footer_name: str):
        _NumberedCanvas.footer_name = clean(footer_name)
        self.c = _NumberedCanvas(str(path), pagesize=(PAGE_W, PAGE_H))
        self.y = TOP

    # primitives (reportlab origin is bottom-left, same as the workflow's PDF)
    def fill_rect(self, x, yb, w, h, col):
        self.c.setFillColor(col)
        self.c.rect(x, yb, w, h, stroke=0, fill=1)

    def stroke_rect(self, x, yb, w, h, col, lw=0.8):
        self.c.setStrokeColor(col)
        self.c.setLineWidth(lw)
        self.c.rect(x, yb, w, h, stroke=1, fill=0)

    def hline(self, x1, x2, yy, col, lw=0.5):
        self.c.setStrokeColor(col)
        self.c.setLineWidth(lw)
        self.c.line(x1, yy, x2, yy)

    def vline(self, xx, y1, y2, col, lw=0.5):
        self.c.setStrokeColor(col)
        self.c.setLineWidth(lw)
        self.c.line(xx, y1, xx, y2)

    def txt(self, x, yy, s, size, font, col):
        self.c.setFillColor(col)
        self.c.setFont(font, size)
        self.c.drawString(x, yy, clean(s))

    def link_line(self, x, baseline, s, size, uri):
        w = tw(s, size)
        self.txt(x, baseline, s, size, F1, COL["blue"])
        self.hline(x, x + w, baseline - 1.5, COL["blue"], 0.4)
        self.c.linkURL(clean(uri), (x, baseline - 2.5, x + w, baseline + size), relative=0, thickness=0)

    def new_page(self):
        self.c.showPage()
        self.y = TOP

    def ensure(self, h):
        if self.y - h < BOTTOM:
            self.new_page()

    # layout blocks
    def section(self, title):
        self.ensure(50)
        self.y -= 16
        self.fill_rect(LEFT, self.y - 22, CW, 22, COL["band"])
        self.fill_rect(LEFT, self.y - 22, 4, 22, COL["blue"])
        self.txt(LEFT + 14, self.y - 15, title.upper(), 10, F2, COL["navy"])
        self.y -= 30

    def para(self, text, size=9, font=F1, color=None, indent=0, lead=None):
        color = color or COL["ink"]
        lead = lead or size + 4
        for ln in wrap(text, CW - indent - PAD_R, size, font == F2):
            self.ensure(lead)
            self.txt(LEFT + indent, self.y - size, ln, size, font, color)
            self.y -= lead

    def labeled(self, label, text, size=8.5, indent=0, off=56, bold=False, color=None, link=False):
        lx = LEFT + indent
        vx = lx + off
        lead = size + 4
        uri = clean(text).split(" ")[0]
        for i, ln in enumerate(wrap(text, RIGHT - PAD_R - vx, size, bold)):
            self.ensure(lead)
            if i == 0:
                self.txt(lx, self.y - size, label, size, F1, COL["muted"])
            if link:
                self.link_line(vx, self.y - size, ln, size, uri)
            else:
                self.txt(vx, self.y - size, ln, size, F2 if bold else F1, color or COL["ink"])
            self.y -= lead

    def kv(self, k, v, boxed=False, alt=False):
        size, vx = 9, LEFT + 150
        lines = wrap(fmt(v), RIGHT - PAD_R - vx, size)
        h = len(lines) * 12 + 8
        self.ensure(h)
        if boxed:
            if alt:
                self.fill_rect(LEFT, self.y - h, CW, h, COL["band"])
            self.vline(LEFT, self.y, self.y - h, COL["line"], 0.8)
            self.vline(RIGHT, self.y, self.y - h, COL["line"], 0.8)
        self.hline(LEFT, RIGHT, self.y - h, COL["line"], 0.5)
        self.txt(LEFT + 10, self.y - 13, k, 8.5, F1, COL["muted"])
        for i, ln in enumerate(lines):
            self.txt(vx, self.y - 13 - i * 12, ln, size, F1, COL["ink"])
        self.y -= h

    def stat_cards(self, items):
        gap = 12
        gw = (CW - gap * (len(items) - 1)) / len(items)
        h = 58
        self.ensure(h + 16)
        for i, it in enumerate(items):
            x = LEFT + i * (gw + gap)
            ls = 7.5 if tw(it["label"], 8.5) > gw - 28 else 8.5
            self.fill_rect(x, self.y - h, gw, h, COL["white"])
            self.stroke_rect(x, self.y - h, gw, h, COL["line"], 0.8)
            self.fill_rect(x, self.y - h, 4, h, it["color"])
            self.txt(x + 16, self.y - 30, it["value"], 22, F2, it["color"])
            self.txt(x + 16, self.y - 47, it["label"], ls, F1, COL["muted"])
        self.y -= h + 14

    def callout(self, text, color, bg, size=8.5, bold=False):
        lead = size + 3.5
        lines = wrap(text, CW - 16 - PAD_R, size, bold)
        h = len(lines) * lead + 16
        self.ensure(h + 8)
        self.fill_rect(LEFT, self.y - h, CW, h, bg)
        self.fill_rect(LEFT, self.y - h, 4, h, color)
        for i, ln in enumerate(lines):
            self.txt(LEFT + 16, self.y - 12 - i * lead, ln, size, F2 if bold else F1, COL["ink"])
        self.y -= h + 10


def generate_evidence_pdf(r: dict, case_ref: str, out_path: Path | None = None) -> Path:
    """Write the evidence PDF for screening result `r` (the dict returned by engine.screen)."""
    app = r["applicant"]
    out_path = out_path or (EVIDENCE_DIR / f"evidence_{re.sub(r'[^A-Za-z0-9_-]', '_', case_ref)}.pdf")
    d = _Doc(out_path, app["name"])
    d.c.setTitle(clean(f"Sanctions screening evidence {case_ref}"))

    # header banner
    d.fill_rect(0, PAGE_H - 92, PAGE_W, 92, COL["navy"])
    d.fill_rect(0, PAGE_H - 96, PAGE_W, 4, COL["blue"])
    d.txt(LEFT, PAGE_H - 42, "SANCTIONS SCREENING", 20, F2, COL["white"])
    d.txt(LEFT, PAGE_H - 62, "Evidence Report", 12, F1, COL["soft"])
    d.txt(LEFT, PAGE_H - 80, "Screened " + fmt_date(r["screened_at"]), 8.5, F1, COL["soft"])
    id_tag = "Case " + case_ref
    d.txt(RIGHT - tw(id_tag, 8.5), PAGE_H - 80, id_tag, 8.5, F1, COL["soft"])
    d.y = PAGE_H - 96 - 18

    d.callout("RESULT: POTENTIAL MATCH. Manual review required.", COL["amber"], COL["amber_bg"], 11, True)

    sh, mh = r["sanctions_hit_count"], r["media_hit_count"]
    d.stat_cards([
        {"label": "Sanctions / watch-list matches" + (" (top 50)" if r["truncated"] else ""),
         "value": num(sh), "color": COL["red"] if sh > 0 else COL["green"]},
        {"label": "Adverse media articles", "value": num(mh), "color": COL["amber"] if mh > 0 else COL["green"]},
        {"label": "Records screened", "value": num(r["total_records"]), "color": COL["blue"]},
    ])

    d.section("Applicant")
    d.kv("Name screened", app["name"])
    d.kv("Date of birth", app["dob"])
    d.kv("Nationality", app["nationality"])
    if app.get("cnic"):
        d.kv("CNIC", app["cnic"])
    if app.get("father_name"):
        d.kv("Father / husband name", app["father_name"])

    d.section("Screening details")
    d.kv("Screened at", fmt_date(r["screened_at"]))
    d.kv("Case reference", case_ref)
    d.kv("Match threshold", f"{r['threshold']:g}%")
    d.kv("Records screened", num(r["total_records"]))
    d.kv("Sanctions / watch-list matches", str(sh) + (" (top 50 shown)" if r["truncated"] else ""))
    d.kv("Adverse media articles", str(mh))

    d.section("Lists screened")
    list_names = [clean(x["list"]) for x in r["lists"]]
    per_list_reliable = all(clean(m["list"]) in list_names for m in r["matches"])
    for lst in r["lists"]:
        recs = num(lst["records"]) + " records"
        loaded = int(lst["records"] or 0) > 0
        status = lst.get("status")
        d.ensure(60)
        d.txt(LEFT + 10, d.y - 14, fit_text(lst["list"], 10, True, CW - 30 - tw(recs, 9) - PAD_R), 10, F2, COL["ink"])
        d.txt(RIGHT - PAD_R - tw(recs, 9), d.y - 14, recs, 9, F1, COL["muted"])
        d.y -= 22
        src = fmt(lst.get("source"))
        d.labeled("Source", src, indent=10, link=bool(re.match(r"^https?://", clean(src), re.I)))
        d.labeled("List date", fmt_date(lst.get("published") or "n/a"), indent=10)
        if not loaded:
            why = re.sub(r"^not available: ", "", str(status or ""), flags=re.I)
            d.labeled("Status", "Not screened. " + (why or "This list could not be retrieved."),
                      indent=10, bold=True, color=COL["red"])
        else:
            if status and status != "OK" and not re.match(r"^not available", str(status), re.I):
                d.labeled("Status", status, indent=10, bold=True, color=COL["red"])
            if per_list_reliable:
                n = sum(1 for m in r["matches"] if clean(m["list"]) == clean(lst["list"]))
                if n == 0:
                    d.labeled("Result", "Screened. No match at the selected threshold.", indent=10, bold=True, color=COL["green"])
                else:
                    d.labeled("Result", f"Screened. {n} potential match{'es' if n > 1 else ''} found.",
                              indent=10, bold=True, color=COL["red"])
            else:
                d.labeled("Result", "Screened.", indent=10, bold=True, color=COL["green"])
        d.y -= 4
        d.hline(LEFT, RIGHT, d.y, COL["line"], 0.5)
        d.y -= 6

    am = r.get("adverse_media")
    if am:
        d.ensure(50)
        d.txt(LEFT + 10, d.y - 14, fit_text("Adverse media: " + am["source"], 10, True, CW - 30), 10, F2, COL["ink"])
        d.y -= 22
        d.labeled("Query", str(am["query"]), indent=10)
        d.labeled("Reviewed", f"{am['articles_reviewed']} articles, status {am['status']}", indent=10)
        d.y -= 6

    d.y -= 4
    d.callout("METHOD. Names are normalised (accents, punctuation and titles removed) and compared token by token "
              "against every primary name and alias (including names and aliases read from the FIA Red Book) using "
              "Jaro-Winkler similarity. A score at or above the threshold is reported as a potential match. A "
              "potential match is not a confirmed identity match. Verify date of birth, nationality and identifiers "
              "before deciding.", COL["blue"], COL["band"], 8.5)

    d.section("Sanctions / watch-list matches")
    if not r["matches"]:
        d.para("None at the selected threshold.", indent=10, color=COL["green"], font=F2)
    for i, m in enumerate(r["matches"]):
        score = float(m["score"])
        sc = COL["red"] if score >= 95 else COL["amber"]
        rows = [
            ("List", m["list"]), ("Reference", m["id"]), ("Matched name / alias", m["matched_name"]),
            ("Type", m["type"]), ("Programme / regime", m["programs"]), ("Date(s) of birth", m["dob"]),
            ("DOB year matches applicant", m["dob_year_match"]), ("Nationality", m["nationality"]),
            ("Listed on", m["listed_on"]),
        ]
        if m.get("cnic"):
            rows.append(("CNIC", m["cnic"]))
        if m.get("cnic_match") is not None:
            rows.append(("CNIC matches applicant", "Yes" if m["cnic_match"] else "No"))
        if m.get("father_name"):
            rows.append(("Father / husband", m["father_name"]))
        if m.get("father_match") is not None:
            rows.append(("Father's name matches applicant", "Yes" if m["father_match"] else "No"))
        if m.get("aliases"):
            rows.append(("Other names", "; ".join(m["aliases"])))
        if m.get("remarks"):
            rows.append(("Remarks", m["remarks"]))
        badge = f"SCORE {score:g}%"
        bw = tw(badge, 8.5, True) + 16
        d.ensure(26 + 44)
        d.fill_rect(LEFT, d.y - 26, CW, 26, COL["navy"])
        d.txt(LEFT + 10, d.y - 17, fit_text(f"{i + 1}. {m['primary_name']}", 10.5, True, CW - bw - 40), 10.5, F2, COL["white"])
        d.fill_rect(RIGHT - bw - 8, d.y - 21, bw, 16, sc)
        d.txt(RIGHT - bw, d.y - 16, badge, 8.5, F2, COL["white"])
        d.y -= 26
        for j, (k, v) in enumerate(rows):
            d.kv(k, v, boxed=True, alt=j % 2 == 1)
        d.y -= 14

    d.section("Adverse media (open news search)")
    d.callout("News articles whose title or summary contains the applicant's surname, at least one more part of the "
              "name, and an adverse keyword. News hits are unverified leads. The person in the article may be someone "
              "else with the same name.", COL["amber"], COL["amber_bg"], 8.5)
    hits = (am or {}).get("hits") or []
    if not hits:
        d.para("None found.", indent=10, color=COL["green"], font=F2)
    for i, h in enumerate(hits):
        inner = CW - 14 - PAD_R - 4
        tl = wrap(f"{i + 1}. {fmt(h['title'])}", inner, 9.5, True)
        ml = wrap(f"Source: {fmt(h['source'])}   |   Published: {fmt(h['published'])}   |   Keyword: {fmt(h['keyword'])}",
                  inner, 8.5)
        link_x = LEFT + 14 + 30
        ll = wrap(fmt(h["link"]), RIGHT - PAD_R - link_x, 7.5)
        hh = 8 + len(tl) * 13 + len(ml) * 11.5 + len(ll) * 10.5 + 12
        d.ensure(hh + 6)
        d.fill_rect(LEFT, d.y - hh, 4, hh, COL["amber"])
        yy = d.y - 8
        for ln in tl:
            d.txt(LEFT + 14, yy - 9.5, ln, 9.5, F2, COL["ink"])
            yy -= 13
        for ln in ml:
            d.txt(LEFT + 14, yy - 8.5, ln, 8.5, F1, COL["muted"])
            yy -= 11.5
        d.txt(LEFT + 14, yy - 7.5, "Link", 7.5, F1, COL["muted"])
        for ln in ll:
            d.link_line(link_x, yy - 7.5, ln, 7.5, fmt(h["link"]))
            yy -= 10.5
        d.y -= hh
        d.hline(LEFT, RIGHT, d.y, COL["line"], 0.5)
        d.y -= 8

    # reviewer decision block
    d.ensure(108)
    d.y -= 16
    d.fill_rect(LEFT, d.y - 90, CW, 90, COL["band"])
    d.stroke_rect(LEFT, d.y - 90, CW, 90, COL["line"], 0.8)
    d.txt(LEFT + 14, d.y - 20, "REVIEWER DECISION", 10, F2, COL["navy"])
    d.stroke_rect(LEFT + 14, d.y - 44, 11, 11, COL["ink"], 0.9)
    d.txt(LEFT + 32, d.y - 42, "True match", 9.5, F1, COL["ink"])
    d.stroke_rect(LEFT + 130, d.y - 44, 11, 11, COL["ink"], 0.9)
    d.txt(LEFT + 148, d.y - 42, "False positive", 9.5, F1, COL["ink"])
    d.hline(LEFT + 14, LEFT + 240, d.y - 72, COL["ink"], 0.6)
    d.txt(LEFT + 14, d.y - 83, "Reviewer", 8, F1, COL["muted"])
    d.hline(LEFT + 280, LEFT + 420, d.y - 72, COL["ink"], 0.6)
    d.txt(LEFT + 280, d.y - 83, "Date", 8, F1, COL["muted"])
    d.y -= 90

    d.c.showPage()
    d.c.save()
    return out_path
