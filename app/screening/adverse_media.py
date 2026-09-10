"""
Adverse media / negative news screening.

Three modes, tried in this order:
  1. VENDOR MODE — call a real screening API (Sanction Scanner / WorldAML /
     ComplyAdvantage / etc). Structured, de-duplicated, auditable. Set
     ADVERSE_MEDIA_API_KEY to use this.
  2. CLAUDE SEARCH MODE — uses Claude's web search tool via the Anthropic
     API to search for adverse media and returns a structured verdict with
     real cited sources (screenshotted as evidence). Cheaper than most
     vendors, legitimate use of a search API (not scraping search-results
     pages), but it's live web search rather than a curated compliance
     database. Set ANTHROPIC_API_KEY to use this.
  3. RAW SEARCH+SCREENSHOT MODE (last-resort fallback) — screenshots a
     search-results page directly. Noisiest option; always routes to
     manual review. Used only if neither of the above is configured.
"""

import os
from pathlib import Path
from datetime import datetime, timezone
import requests

VENDOR_API_KEY = os.environ.get("ADVERSE_MEDIA_API_KEY")
VENDOR_ENDPOINT = os.environ.get("ADVERSE_MEDIA_ENDPOINT", "https://api.vendor.example.com/v1/adverse-media")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def check_via_vendor(applicant_name: str) -> dict:
    """Real vendor call — replace URL/payload/response parsing with your vendor's actual API contract."""
    resp = requests.post(
        VENDOR_ENDPOINT,
        headers={"Authorization": f"Bearer {VENDOR_API_KEY}"},
        json={"name": applicant_name},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    # Adapt this parsing block to your vendor's actual response schema.
    return {
        "matched_entry": data.get("top_match_name"),
        "score": data.get("confidence_score"),
        "detail": data.get("summary", "See vendor dashboard for full report."),
        "source_url": data.get("report_url"),
    }


def check_via_claude_search(applicant_name: str, screenshot_out_dir: Path | None = None) -> dict:
    """
    Uses Claude's web search tool (via the Anthropic API) to search for
    adverse media, then optionally screenshots the actual cited source
    pages — better evidence than screenshotting a raw search-results page,
    since it shows the real article Claude found.

    Pricing (check docs.claude.com for current rates): ~$10 per 1,000
    searches plus token costs. Cheaper than most AML vendors' adverse-media
    add-on, but it's live web search, not a curated PEP/crime database —
    treat it as a reasonable pilot-stage option, not a full replacement for
    a compliance-grade vendor once you're past pilot volume.

    Requires: pip install anthropic
    Env var: ANTHROPIC_API_KEY
    """
    import anthropic
    import json as _json

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    system_prompt = (
        "You are an AML/compliance research assistant. Search for adverse "
        "media about the named individual: news of fraud, corruption, money "
        "laundering, terrorism financing, sanctions violations, human "
        "trafficking, or other financial crime allegations or convictions. "
        "Prioritize Pakistani sources but include international coverage. "
        "Respond with ONLY a JSON object (no other text, no markdown fences) "
        "matching this shape: "
        '{"risk_flag": true|false, "confidence": 0-100, "summary": "...", '
        '"matches": [{"description": "...", "source_url": "..."}]}. '
        "If you find nothing credible, set risk_flag to false and matches to []. "
        "Be conservative — common names produce false positives; only flag "
        "credible, specific matches, not vague name similarity."
    )

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": f'Search for adverse media on: "{applicant_name}"'}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    )

    # Collect the final text block (the JSON verdict) and citation URLs
    # from any cited text blocks, for evidence capture.
    final_text = ""
    cited_urls = []
    for block in response.content:
        if block.type == "text":
            final_text += block.text
            for citation in (getattr(block, "citations", None) or []):
                url = getattr(citation, "url", None)
                if url and url not in cited_urls:
                    cited_urls.append(url)

    try:
        verdict = _json.loads(final_text.strip())
    except (_json.JSONDecodeError, ValueError):
        return {
            "matched_entry": None,
            "score": None,
            "detail": f"Could not parse Claude's response as JSON. Raw: {final_text[:300]}",
            "source_url": None,
            "status": "ERROR",
        }

    matches = verdict.get("matches", [])
    top_match = matches[0] if matches else None
    status = "REVIEW" if verdict.get("risk_flag") else "CLEAR"

    screenshot_path = None
    if status == "REVIEW" and cited_urls and screenshot_out_dir is not None:
        screenshot_path = _capture_screenshot(cited_urls[0], screenshot_out_dir / "claude_media.png")

    return {
        "matched_entry": top_match["description"] if top_match else None,
        "score": verdict.get("confidence"),
        "detail": verdict.get("summary", "No summary provided."),
        "source_url": cited_urls[0] if cited_urls else None,
        "all_sources": cited_urls,
        "status": status,
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
    }


def _capture_screenshot(url: str, out_path: Path) -> Path | None:
    """Screenshots one specific URL (a real cited article, not a search page)."""
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, timeout=20000)
            page.wait_for_timeout(1000)
            page.screenshot(path=str(out_path), full_page=True)
            browser.close()
        return out_path
    except Exception:
        return None


def check_via_search_and_screenshot(applicant_name: str, screenshot_out_path: Path) -> dict:
    """
    Fallback demo path: no vendor configured. Runs a news search and
    captures a screenshot of the results page as the evidence artifact.
    Requires `playwright install chromium` to have been run once.
    """
    from playwright.sync_api import sync_playwright

    query = f'"{applicant_name}" fraud OR corruption OR sanctions OR investigation'
    search_url = f"https://www.bing.com/news/search?q={requests.utils.quote(query)}"

    result = {
        "matched_entry": None,
        "score": None,
        "detail": "No vendor API configured — ran a raw news search as a fallback. "
                   "This is noisier than a structured adverse-media vendor feed; "
                   "treat any result here as needing manual review, not an automated hit.",
        "source_url": search_url,
        "screenshot_path": None,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(search_url, timeout=20000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(screenshot_out_path), full_page=True)
            browser.close()
        result["screenshot_path"] = str(screenshot_out_path)
        result["status"] = "REVIEW"  # always route to human review in fallback mode
    except Exception as e:
        result["detail"] += f" (screenshot capture failed: {e})"
        result["status"] = "ERROR"

    return result


def check(applicant_name: str, screenshot_out_path: Path | None = None) -> dict:
    if VENDOR_API_KEY:
        return check_via_vendor(applicant_name)
    if ANTHROPIC_API_KEY:
        out_dir = screenshot_out_path.parent if screenshot_out_path else None
        return check_via_claude_search(applicant_name, screenshot_out_dir=out_dir)
    if screenshot_out_path is not None:
        return check_via_search_and_screenshot(applicant_name, screenshot_out_path)
    return {
        "matched_entry": None,
        "score": None,
        "detail": "Adverse media check skipped — no vendor key, no ANTHROPIC_API_KEY, "
                   "and no screenshot path provided.",
        "source_url": None,
        "status": "SKIPPED",
    }
