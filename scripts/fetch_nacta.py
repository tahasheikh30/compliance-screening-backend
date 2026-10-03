#!/usr/bin/env python3
"""
Download the NACTA Proscribed Persons list with a real browser and (optionally) upload it to the
screening backend.

Why a browser: nfs.nacta.gov.pk is a Blazor Server app. Its page talks to the server over a private
SignalR connection, and its JSON / Excel / XML buttons build the file inside that session, so there
is no web address a program can fetch. A browser can click the button, so this script does that.

    pip install -r scripts/requirements-nacta.txt
    python -m playwright install chromium

    # save the file and check it, without uploading anything
    python scripts/fetch_nacta.py

    # also upload it to the backend (set these, or pass --api-url and --api-key)
    SCREENING_API_URL=https://your-service.onrender.com SCREENING_API_KEY=... python scripts/fetch_nacta.py --upload

    # watch it work (a visible browser window), useful the first time
    python scripts/fetch_nacta.py --show

Exit codes: 0 done, 1 could not download, 2 the file could not be read or looks incomplete (too few people, or far fewer than the page shows),
3 the upload was refused.

If NACTA changes the page and the button cannot be found, the script saves nacta_debug.png and prints
what it could see, so the cause can be fixed quickly.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_URL = "https://nfs.nacta.gov.pk/"
MIN_EXPECTED = 1000          # the Fourth Schedule has several thousand people; far fewer means a partial export
COUNT_WARN = 0.02            # a difference from the count shown on the page above this is reported
COUNT_FAIL = 0.10            # above this the file is refused: it is probably partial and must not replace a good list


def fetch_with_browser(url: str, fmt: str, out_dir: Path, timeout_s: int, show: bool) -> tuple:
    """Returns (path of the downloaded file, the 'Total Results' count shown on the page or None)."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not show)
        context = browser.new_context(accept_downloads=True, locale="en-US", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            # A Blazor Server page draws itself only once its connection to the server is up, so wait
            # for something the finished page shows rather than for the page load.
            page.get_by_text(re.compile(r"Total\s+Results", re.I)).first.wait_for(timeout=timeout_s * 1000)
            body = page.inner_text("body")
            m = re.search(r"Total\s+Results\s*:?\s*([\d,]+)", body, re.I)
            total = int(m.group(1).replace(",", "")) if m else None

            # the label may be styled upper case, so match it ignoring case
            button = page.get_by_text(re.compile(rf"^\s*{fmt}\s*$", re.I)).first
            with page.expect_download(timeout=timeout_s * 1000) as info:
                button.click()
            download = info.value
            name = download.suggested_filename or f"nacta.{fmt}"
            path = out_dir / re.sub(r"[^A-Za-z0-9._-]", "_", name)
            download.save_as(path)
            return path, total
        except Exception:
            try:
                page.screenshot(path=str(out_dir / "nacta_debug.png"), full_page=True)
                labels = [t.strip() for t in page.locator("button, a").all_inner_texts() if t.strip()][:40]
                print(f"Could not get the file. Saved {out_dir / 'nacta_debug.png'}.", file=sys.stderr)
                print(f"Buttons and links visible on the page: {labels}", file=sys.stderr)
            except Exception:
                pass
            raise
        finally:
            browser.close()


def check_file(path: Path, page_total: int | None, min_records: int) -> tuple:
    """Reads the file with the same parser the backend uses. Returns (record count, list of warnings)."""
    from app.screening import parsers

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    records, info = parsers.parse_nacta_persons(text)     # raises ValueError with a clear message
    warnings = []
    if len(records) < min_records:
        raise ValueError(f"Only {len(records)} people were found, fewer than the {min_records} expected. "
                         "The download may be partial, so it was not used.")
    if page_total:
        gap = abs(len(records) - page_total) / page_total
        if gap > COUNT_FAIL:
            raise ValueError(f"The page shows {page_total:,} results but the file holds only {len(records):,}. "
                             "The download is probably partial, so it was not used.")
        if gap > COUNT_WARN:
            warnings.append(f"The page showed {page_total:,} results but the file holds {len(records):,}.")
    if info["with_cnic"] < len(records) * 0.5:
        warnings.append(f"Only {info['with_cnic']} of {len(records)} people have a usable CNIC.")
    return len(records), warnings


def upload(path: Path, api_url: str, api_key: str) -> dict:
    url = api_url.rstrip("/") + "/api/admin/nacta?filename=" + urllib.request.quote(path.name)
    req = urllib.request.Request(url, data=path.read_bytes(), method="POST", headers={
        "X-API-Key": api_key,
        "Content-Type": "application/json" if path.suffix.lower() == ".json" else "application/octet-stream",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download the NACTA Proscribed Persons list and optionally upload it.")
    ap.add_argument("--url", default=DEFAULT_URL, help="the NACTA page (default %(default)s)")
    ap.add_argument("--format", default="json", choices=["json", "xml", "excel"],
                    help="which export button to click (default json; excel is saved but cannot be uploaded)")
    ap.add_argument("--out", default="nacta_download", help="folder for the downloaded file")
    ap.add_argument("--timeout", type=int, default=120, help="seconds to wait for the page and the download")
    ap.add_argument("--min-records", type=int, default=MIN_EXPECTED)
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("--upload", action="store_true", help="upload the file to the screening backend")
    ap.add_argument("--api-url", default=os.environ.get("SCREENING_API_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("SCREENING_API_KEY", ""))
    args = ap.parse_args(argv)

    if args.upload and not (args.api_url and args.api_key):
        print("--upload needs SCREENING_API_URL and SCREENING_API_KEY (or --api-url and --api-key).", file=sys.stderr)
        return 3

    try:
        path, total = fetch_with_browser(args.url, args.format, Path(args.out), args.timeout, args.show)
    except Exception as exc:
        print(f"Could not download the NACTA list: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 1
    print(f"Downloaded {path} ({path.stat().st_size:,} bytes)" + (f"; the page shows {total:,} results" if total else ""))

    if args.format == "excel":
        print("Excel files are saved but not checked or uploaded. Use --format json for that.")
        return 0
    try:
        count, warnings = check_file(path, total, args.min_records)
    except ValueError as exc:
        print(f"The downloaded file was not used: {exc}", file=sys.stderr)
        return 2
    print(f"The file holds {count:,} people.")
    for w in warnings:
        print(f"Warning: {w}")

    if args.upload:
        try:
            result = upload(path, args.api_url, args.api_key)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            print(f"The backend refused the upload (HTTP {exc.code}): {body}", file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"The upload failed: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
            return 3
        print(f"Uploaded. The backend now holds {result.get('records'):,} people (loaded {result.get('uploaded_at')}).")
        for w in result.get("warnings", []):
            print(f"Backend warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
