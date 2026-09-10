"""
Adverse media / negative news screening.

Two modes:
  1. VENDOR MODE (recommended for production) — call a real screening API
     (Sanction Scanner / WorldAML / ComplyAdvantage / etc). Structured,
     de-duplicated, and auditable. Plug your API key into ADVERSE_MEDIA_API_KEY.
  2. SEARCH+SCREENSHOT MODE (fallback / demo) — runs a search and captures
     a screenshot of the results page as evidence. This is noisier and
     less reliable than a vendor feed, but useful when no vendor contract
     is in place yet.

Both modes are wired here so you can switch by setting VENDOR_API_KEY.
"""

import os
from pathlib import Path
from datetime import datetime, timezone
import requests

VENDOR_API_KEY = os.environ.get("ADVERSE_MEDIA_API_KEY")
VENDOR_ENDPOINT = os.environ.get("ADVERSE_MEDIA_ENDPOINT", "https://api.vendor.example.com/v1/adverse-media")


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
    if screenshot_out_path is not None:
        return check_via_search_and_screenshot(applicant_name, screenshot_out_path)
    return {
        "matched_entry": None,
        "score": None,
        "detail": "Adverse media check skipped — no vendor key and no screenshot path provided.",
        "source_url": None,
    }
