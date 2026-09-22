"""
FastAPI backend for the account-opening screening tool.

Run:
    uvicorn app.main:app --reload --port 8000

All endpoints except /api/health require an X-API-Key header matching the
API_KEY env var (see README "Security" section).

Endpoints:
    POST /api/screen               -> run all checks for one applicant (rate limited)
    GET  /api/applicants            -> list past screenings
    GET  /api/applicants/{id}       -> full result detail for one applicant
    GET  /api/evidence/{result_id}  -> download the evidence PDF for a hit
    GET  /api/admin/near-misses     -> audit log of scores that came close to a threshold without crossing it
    GET  /api/admin/activity        -> audit log of admin actions (uploads, activations, refreshes)
    POST /api/admin/refresh-unsc    -> refresh the UNSC cache
    POST /api/admin/refresh-ofac    -> refresh the OFAC SDN + Consolidated Non-SDN caches
    POST /api/admin/refresh-uksl    -> refresh the UK Sanctions List cache
    POST /api/admin/refresh         -> combined refresh of every source with a free automated feed (UNSC, OFAC,
                                        UKSL, FIA scrape) — partial failures are reported per-source, not fatal

    FIA Red Book — edition registry (see app/screening/fia_redbook.py):
    GET    /api/admin/fia-redbook/editions             -> list every staged/active/archived edition
    POST   /api/admin/fia-redbook/editions              -> upload a PDF; stages it, does NOT activate it
    GET    /api/admin/fia-redbook/editions/{id}         -> one edition's metadata
    DELETE /api/admin/fia-redbook/editions/{id}         -> discard a STAGED edition (active/archived can't be deleted)
    GET    /api/admin/fia-redbook/editions/{id}/entries -> paginated, searchable parsed names, for spot-checking
    GET    /api/admin/fia-redbook/editions/{id}/diff    -> names added/removed vs. the active edition
    GET    /api/admin/fia-redbook/editions/{id}/page/{n} -> PNG of one PDF page, for side-by-side review
    POST   /api/admin/fia-redbook/editions/{id}/activate -> make this edition live (or roll back to a past one)
    POST   /api/admin/fia-redbook/check                 -> look for a new Red Book on fia.gov.pk; stages it (does not auto-activate)
    GET    /api/admin/fia-redbook/status                -> legacy: what's currently live (kept for back-compat)
    POST   /api/admin/fia-redbook/upload                -> legacy: upload + activate in one call (kept for back-compat)
"""

from pathlib import Path
from datetime import datetime, timezone

import os

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from app import database as db
from app.config import SCREENSHOT_DIR, MATCH_THRESHOLD, REVIEW_THRESHOLD
from app.schemas import (
    ScreenRequest, ScreenResponse, ScreeningResultOut, ApplicantSummary, NearMissEntry,
    AdminAuditEntry, ActivateEditionRequest,
)
from app.screening import unsc, ofac, uksl, fia_redbook, adverse_media
from app import evidence
from app.auth import require_api_key
from app.errors import AppError, configure_logging, install as install_error_handlers, request_context, logger

configure_logging()

from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: E402  (after logging config, before app)
from slowapi.util import get_remote_address  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Account Screening API")
app.state.limiter = limiter
install_error_handlers(app)  # registers handlers for AppError, HTTPException, 422, 429, and a catch-all

# Comma-separated list in env, e.g.:
#   ALLOWED_ORIGINS=https://your-app.vercel.app,https://your-app-git-main.vercel.app
_default_origins = "http://localhost:5173"
allowed_origins = os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",")

# Middleware registration order matters, and it's not the intuitive direction.
# Starlette.add_middleware() PREPENDS: the LAST middleware registered ends up
# OUTERMOST (it runs first on the way in, last on the way out).
#
# Also: Starlette pulls any handler registered for the bare `Exception` class
# out of ExceptionMiddleware and gives it to ServerErrorMiddleware instead —
# which wraps EVERYTHING, including CORSMiddleware. So a plain
# @app.exception_handler(Exception) response (our final backstop, registered
# by install_error_handlers above) can never carry CORS headers, no matter
# what order anything below is in — that's a Starlette/FastAPI limitation,
# not something app code can fix.
#
# request_context's own try/except is what actually produces a CORS-safe
# error response in the normal case: it catches the exception ITSELF (before
# it can reach ServerErrorMiddleware) and returns a normal JSONResponse, which
# then flows back up through every middleware registered after it — so
# request_context must be registered BEFORE (i.e. end up INNER to)
# security_headers and CORS. Get this order backwards and every 500 comes
# back with no Access-Control-Allow-Origin header, which the browser reports
# to the frontend as an opaque "Failed to fetch" instead of the real error —
# see tests/test_api.py::test_500_response_still_has_cors_header.
app.middleware("http")(request_context)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # This API only ever returns JSON/PDF containing applicant PII — never cache it.
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
# NOTE: do not also register slowapi's default RateLimitExceeded handler here —
# install_error_handlers() above already registered ours (structured JSON body
# with a code/hint), and registering another one for the same exception type
# would silently replace it.


@app.on_event("startup")
def startup():
    db.init_db()
    fia_redbook.init_registry()
    if not os.environ.get("API_KEY"):
        logger.warning(
            "API_KEY is not set. Every protected endpoint will return 503 "
            "until it is configured. Do not deploy like this."
        )


def _status_for(result: dict) -> str:
    """
    Turns a source-check result dict into HIT / REVIEW / CLEAR / NOT_CONFIGURED.

    Three rules that override a bare score comparison:
      - A source that raised an exception (network/parsing/library failure —
        see _run_source_check) must NOT be reported as CLEAR or even as the
        quieter NOT_CONFIGURED — ERROR is its own status so the frontend can
        show it distinctly from "nobody has set this source up yet".
      - A source that couldn't actually run because it's simply not set up
        (cache empty, no API key) must NOT be reported as CLEAR either — that
        would silently hide the fact that the check never happened.
      - An exact CNIC match is direct identity evidence, not a fuzzy
        inference, so it always resolves to HIT regardless of the name
        score — a shared 13-digit national ID number should never be
        outvoted by a mediocre fuzzy name score.
    """
    if result.get("status") == "ERROR":
        return "ERROR"
    if result.get("available") is False:
        return "NOT_CONFIGURED"
    if result.get("cnic_match"):
        return "HIT"
    score = result.get("score")
    if score is None:
        return "CLEAR"
    if score >= MATCH_THRESHOLD:
        return "HIT"
    if score >= REVIEW_THRESHOLD:
        return "REVIEW"
    return "CLEAR"


def _run_source_check(source_key: str, fn, *args, **kwargs) -> dict:
    """
    Runs one screening source's check() and NEVER lets it take down the whole
    /api/screen request. If it raises, the applicant still gets a result row
    for this source — status ERROR, with a human-readable reason — instead of
    the entire screening (including sources that already succeeded) being
    lost to a 500. describe_exception() (app/errors.py) turns the raw
    exception into a code + message without leaking stack traces to the client.
    """
    from app.errors import describe_exception
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        code, message, hint = describe_exception(exc)
        logger.exception("%s check failed", source_key)
        return {
            "matched_entry": None, "score": None,
            "detail": f"{message} ({code}). {hint}",
            "source_url": None, "available": False, "near_miss": False,
            "cnic_match": False, "status": "ERROR", "list_version": None,
        }


def _log_near_miss_if_any(applicant_id: int, source: str, result: dict, now: str):
    if result.get("near_miss"):
        db.insert_near_miss(
            applicant_id=applicant_id,
            source=source,
            matched_entry=result.get("matched_entry") or (result.get("breakdown") or {}).get("matched_entry_raw"),
            score=result.get("score"),
            threshold=REVIEW_THRESHOLD,
            detail=result.get("detail"),
            logged_at=now,
        )


def _process_structured_source(
    applicant_id: int, req: ScreenRequest, now: str,
    source_key: str, evidence_source_label: str, result: dict,
    statuses: list, results_out: list,
):
    """
    Shared handling for any source whose evidence is purely structured
    data — no webpage/PDF screenshot to embed. UNSC, OFAC, and UKSL all
    fit this shape (a downloaded list, matched by name, nothing to
    photograph); FIA Red Book (renders a PDF page image) and Adverse
    Media (screenshots a webpage) don't, so those two stay as their own
    blocks below rather than being folded into this helper.

    Mutates `statuses` and `results_out` in place (matches the calling
    style already used for FIA Red Book and Adverse Media below).

    If evidence PDF generation itself fails (disk full, bad image, etc.),
    the result is still saved as a HIT — losing the PDF must never lose
    the finding — with the failure appended to `detail` so it's visible,
    not silently dropped.
    """
    status = _status_for(result)
    statuses.append(status)
    _log_near_miss_if_any(applicant_id, source_key, result, now)
    result_id = db.insert_result(
        applicant_id, source_key, result["matched_entry"], result["score"],
        status, result["detail"], None, now,
        cnic_match=result.get("cnic_match", False), near_miss=result.get("near_miss", False),
        list_version=result.get("list_version"),
    )
    detail = result["detail"]
    evidence_file = None
    if status == "HIT":
        try:
            pdf_path = evidence.generate_evidence_pdf(
                req.full_name, req.cnic, evidence_source_label,
                result["matched_entry"], result["score"], result["source_url"],
                image_path=None,  # structured data feed, not a webpage — no screenshot
                result_id=result_id,
                cnic_match=result.get("cnic_match", False),
                breakdown=result.get("breakdown"),
                list_version=result.get("list_version"),
            )
            evidence_file = pdf_path.name
            db.set_evidence_file(result_id, evidence_file)
        except Exception:
            logger.exception("Evidence PDF generation failed for result %s (%s)", result_id, source_key)
            note = " [Evidence PDF could not be generated — see server log for this result's reference ID.]"
            db.append_result_detail(result_id, note)
            detail += note
    results_out.append(ScreeningResultOut(
        id=result_id, source=source_key, matched_entry=result["matched_entry"],
        score=result["score"], status=status, detail=detail,
        evidence_file=evidence_file, checked_at=now,
        cnic_match=result.get("cnic_match", False), near_miss=result.get("near_miss", False),
        list_version=result.get("list_version"),
    ))


@app.post("/api/screen", response_model=ScreenResponse, dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def screen_applicant(request: Request, req: ScreenRequest):
    now = datetime.now(timezone.utc).isoformat()
    applicant_id = db.insert_applicant(req.full_name, req.cnic, req.father_name, now, "PENDING")

    results_out = []
    statuses = []

    # --- UNSC ---
    unsc_result = _run_source_check("UNSC", unsc.check, req.full_name, threshold=REVIEW_THRESHOLD)
    _process_structured_source(
        applicant_id, req, now, "UNSC", "UNSC Consolidated Sanctions List",
        unsc_result, statuses, results_out,
    )

    # --- OFAC (SDN + Consolidated Non-SDN) ---
    ofac_result = _run_source_check("OFAC", ofac.check, req.full_name, threshold=REVIEW_THRESHOLD)
    _process_structured_source(
        applicant_id, req, now, "OFAC", "OFAC Sanctions List (SDN / Consolidated Non-SDN)",
        ofac_result, statuses, results_out,
    )

    # --- UK Sanctions List (FCDO) ---
    uksl_result = _run_source_check("UKSL", uksl.check, req.full_name, threshold=REVIEW_THRESHOLD)
    _process_structured_source(
        applicant_id, req, now, "UKSL", "UK Sanctions List (FCDO)",
        uksl_result, statuses, results_out,
    )

    # --- FIA Red Book ---
    fia_result = _run_source_check(
        "FIA_REDBOOK", fia_redbook.check, req.full_name, applicant_cnic=req.cnic, threshold=REVIEW_THRESHOLD,
    )
    status = _status_for(fia_result)
    statuses.append(status)
    _log_near_miss_if_any(applicant_id, "FIA_REDBOOK", fia_result, now)
    result_id = db.insert_result(
        applicant_id, "FIA_REDBOOK", fia_result["matched_entry"], fia_result["score"],
        status, fia_result["detail"], None, now,
        cnic_match=fia_result.get("cnic_match", False), near_miss=fia_result.get("near_miss", False),
        list_version=fia_result.get("list_version"),
    )
    detail = fia_result["detail"]
    evidence_file = None
    if status == "HIT":
        try:
            image_path = None
            if fia_result.get("page_number") is not None:
                image_path = SCREENSHOT_DIR / f"fia_page_{result_id}.png"
                if fia_redbook.render_matched_page(fia_result["page_number"], image_path) is None:
                    image_path = None  # render failed/edition missing — evidence PDF falls back to text-only
            pdf_path = evidence.generate_evidence_pdf(
                req.full_name, req.cnic, "FIA Red Book",
                fia_result["matched_entry"], fia_result["score"], fia_result["source_url"],
                image_path=image_path, result_id=result_id,
                cnic_match=fia_result.get("cnic_match", False),
                breakdown=fia_result.get("breakdown"),
                list_version=fia_result.get("list_version"),
            )
            evidence_file = pdf_path.name
            db.set_evidence_file(result_id, evidence_file)
        except Exception:
            logger.exception("FIA evidence PDF generation failed for result %s", result_id)
            note = " [Evidence PDF could not be generated — see server log for this result's reference ID.]"
            db.append_result_detail(result_id, note)
            detail += note
    results_out.append(ScreeningResultOut(
        id=result_id, source="FIA_REDBOOK", matched_entry=fia_result["matched_entry"],
        score=fia_result["score"], status=status, detail=detail,
        evidence_file=evidence_file, checked_at=now,
        cnic_match=fia_result.get("cnic_match", False), near_miss=fia_result.get("near_miss", False),
        list_version=fia_result.get("list_version"),
    ))

    # --- Adverse Media ---
    media_screenshot_path = SCREENSHOT_DIR / f"media_{applicant_id}.png"
    media_result = _run_source_check(
        "ADVERSE_MEDIA", adverse_media.check, req.full_name, screenshot_out_path=media_screenshot_path,
    )
    media_status = media_result.get("status") or _status_for(
        {"score": media_result.get("score"), "available": media_result.get("available", True)}
    )
    statuses.append(media_status)
    result_id = db.insert_result(
        applicant_id, "ADVERSE_MEDIA", media_result.get("matched_entry"),
        media_result.get("score"), media_status, media_result["detail"], None, now,
    )
    detail = media_result["detail"]
    evidence_file = None
    if media_status in ("HIT", "REVIEW") and media_result.get("screenshot_path"):
        try:
            pdf_path = evidence.generate_evidence_pdf(
                req.full_name, req.cnic, "Adverse Media Search",
                media_result.get("matched_entry") or "See screenshot — raw search, not a confirmed match",
                media_result.get("score") or 0, media_result.get("source_url"),
                image_path=Path(media_result["screenshot_path"]), result_id=result_id,
            )
            evidence_file = pdf_path.name
            db.set_evidence_file(result_id, evidence_file)
        except Exception:
            logger.exception("Adverse media evidence PDF generation failed for result %s", result_id)
            note = " [Evidence PDF could not be generated — see server log for this result's reference ID.]"
            db.append_result_detail(result_id, note)
            detail += note
    results_out.append(ScreeningResultOut(
        id=result_id, source="ADVERSE_MEDIA", matched_entry=media_result.get("matched_entry"),
        score=media_result.get("score"), status=media_status, detail=detail,
        evidence_file=evidence_file, checked_at=now,
    ))

    # Overall routing — a source that didn't actually run must never
    # resolve to AUTO_CLEAR; that would hide a misconfiguration behind an
    # apparently-clean result.
    if "HIT" in statuses:
        overall = "ESCALATE_TO_COMPLIANCE"
    elif "REVIEW" in statuses:
        overall = "MANUAL_REVIEW"
    elif "NOT_CONFIGURED" in statuses or "ERROR" in statuses:
        overall = "MANUAL_REVIEW"
    else:
        overall = "AUTO_CLEAR"
    db.update_applicant_status(applicant_id, overall)

    return ScreenResponse(
        applicant_id=applicant_id, full_name=req.full_name,
        overall_status=overall, results=results_out,
    )


@app.get("/api/applicants", response_model=list[ApplicantSummary], dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def list_applicants(request: Request):
    return db.list_applicants()


@app.get("/api/applicants/{applicant_id}", response_model=ScreenResponse, dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def get_applicant(request: Request, applicant_id: int):
    applicant = db.get_applicant(applicant_id)
    if not applicant:
        raise HTTPException(404, "Applicant not found")
    results = db.get_results_for_applicant(applicant_id)
    return ScreenResponse(
        applicant_id=applicant_id, full_name=applicant["full_name"],
        overall_status=applicant["overall_status"],
        results=[ScreeningResultOut(**r) for r in results],
    )


@app.get("/api/evidence/{result_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def download_evidence(request: Request, result_id: int):
    result = db.get_result(result_id)
    if not result:
        raise AppError(404, "RESULT_NOT_FOUND", "No screening result with that ID.")
    if not result.get("evidence_file"):
        raise AppError(404, "EVIDENCE_NOT_GENERATED",
                       "This result has no evidence PDF.",
                       "Evidence PDFs are only generated for HITs (and adverse-media REVIEWs). "
                       "If this should be one and isn't, generation may have failed — check the server log.")
    path = evidence.EVIDENCE_DIR / result["evidence_file"]
    if not path.exists():
        raise AppError(404, "EVIDENCE_FILE_MISSING",
                       "The evidence file is recorded but missing on disk.",
                       "The persistent disk may have been reset since this screening ran (check STORAGE_DIR "
                       "in app/config.py / render.yaml). The finding itself is still in the database.")
    return FileResponse(path, media_type="application/pdf", filename=result["evidence_file"])


@app.get("/api/admin/near-misses", response_model=list[NearMissEntry], dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def near_misses(request: Request, limit: int = 200):
    """
    Audit trail of scores that came within NEAR_MISS_MARGIN points of
    REVIEW_THRESHOLD without crossing it — i.e. scored CLEAR, but close
    enough to be worth a compliance analyst's periodic attention. Useful
    for sanity-checking whether the thresholds in app/config.py are set
    where the business actually wants them, using real applicant traffic
    rather than guesswork.
    """
    return db.list_near_misses(limit=limit)


@app.get("/api/admin/activity", response_model=list[AdminAuditEntry], dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def admin_activity(request: Request, limit: int = 100):
    """Who changed what and when: Red Book uploads/activations/discards, and every list refresh."""
    return db.list_admin_actions(limit=limit)


def _log_admin(request: Request, action: str, target: str | None = None, detail: str | None = None):
    db.log_admin_action(
        action, target, detail, request.client.host if request.client else None,
        getattr(request.state, "request_id", None), datetime.now(timezone.utc).isoformat(),
    )


MAX_UPLOAD_BYTES = fia_redbook.MAX_UPLOAD_BYTES  # 20MB — Red Book PDFs are small; this is a generous ceiling


async def _read_upload(file: UploadFile) -> bytes:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise AppError(400, "FIA_WRONG_EXTENSION", "Please upload a PDF file (.pdf extension required).")
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise AppError(413, "FIA_FILE_TOO_LARGE",
                       f"File too large — max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
                       "Compress the PDF (e.g. re-save images at a lower resolution) and try again.")
    if len(contents) == 0:
        raise AppError(400, "FIA_EMPTY_UPLOAD", "The uploaded file is empty (0 bytes).",
                       "Check the file wasn't corrupted while it was being selected or transferred.")
    return contents


@app.post("/api/admin/refresh-unsc", dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
def refresh_unsc_cache(request: Request):
    """Refresh the UNSC sanctions list cache. Safe to call on a daily schedule."""
    result = unsc.refresh_cache()
    _log_admin(request, "refresh_unsc", detail=f"{result.get('bytes', '?')} bytes")
    return result


@app.post("/api/admin/refresh-ofac", dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
def refresh_ofac_cache(request: Request):
    """Refresh the OFAC SDN + Consolidated Non-SDN caches. Safe to call on a daily schedule."""
    result = ofac.refresh_cache()
    _log_admin(request, "refresh_ofac")
    return result


@app.post("/api/admin/refresh-uksl", dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
def refresh_uksl_cache(request: Request):
    """
    Refresh the UK Sanctions List cache. Safe to call on a daily schedule.
    Unlike the other refresh endpoints, this one first re-resolves the
    current CSV download link from the gov.uk publication page (see
    app/screening/uksl.py) — if FCDO changes that page's layout, this
    will start raising instead of silently serving a stale list; watch
    for that failure mode specifically if this endpoint starts erroring.
    """
    result = uksl.refresh_cache()
    _log_admin(request, "refresh_uksl")
    return result


# --- FIA Red Book: edition registry --------------------------------------
# See app/screening/fia_redbook.py for the full model (stage -> review ->
# activate -> archive; rollback is just activating an older edition again).

@app.get("/api/admin/fia-redbook/editions", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def list_fia_editions(request: Request):
    """Every edition ever uploaded/scraped: which is active, which are staged awaiting review, which are archived."""
    return fia_redbook.list_editions()


@app.post("/api/admin/fia-redbook/editions", dependencies=[Depends(require_api_key)])
@limiter.limit("10/hour")
async def create_fia_edition(request: Request, file: UploadFile = File(...), notes: str | None = Form(default=None)):
    """
    Upload a Red Book PDF. This STAGES it — parses and validates it, but does
    NOT make it live. Review it (GET .../entries, .../diff, .../page/{n}) and
    then POST .../activate when you're satisfied.
    """
    contents = await _read_upload(file)
    result = fia_redbook.create_edition(contents, original_filename=file.filename, source="upload", notes=notes)
    _log_admin(request, "fia_edition_staged", target=result["id"],
              detail=f"{result['names_found']} names, {len(result['warnings'])} warning(s)")
    return result


@app.get("/api/admin/fia-redbook/editions/{edition_id}", dependencies=[Depends(require_api_key)])
def get_fia_edition(request: Request, edition_id: str):
    return fia_redbook.get_edition(edition_id)


@app.delete("/api/admin/fia-redbook/editions/{edition_id}", dependencies=[Depends(require_api_key)])
def delete_fia_edition(request: Request, edition_id: str):
    """Discard a STAGED edition (one that was never activated). Active/archived editions are retained for audit."""
    result = fia_redbook.delete_edition(edition_id)
    _log_admin(request, "fia_edition_discarded", target=edition_id)
    return result


@app.get("/api/admin/fia-redbook/editions/{edition_id}/entries", dependencies=[Depends(require_api_key)])
def browse_fia_edition_entries(request: Request, edition_id: str, q: str | None = None,
                               cnic_only: bool = False, offset: int = 0, limit: int = 50):
    """Paginated, searchable view of the names this edition parsed to — the spot-check surface for review."""
    return fia_redbook.browse_entries(edition_id, q=q, cnic_only=cnic_only, offset=offset, limit=limit)


@app.get("/api/admin/fia-redbook/editions/{edition_id}/diff", dependencies=[Depends(require_api_key)])
def diff_fia_edition(request: Request, edition_id: str):
    """Names added/removed compared with whatever edition is currently active."""
    return fia_redbook.diff_against_active(edition_id)


@app.get("/api/admin/fia-redbook/editions/{edition_id}/page/{page_number}", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")
def render_fia_edition_page(request: Request, edition_id: str, page_number: int):
    """PNG of one page (1-based) of this edition's original PDF — for comparing against the parsed entries."""
    png = fia_redbook.render_edition_page(edition_id, page_number)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/admin/fia-redbook/editions/{edition_id}/activate", dependencies=[Depends(require_api_key)])
@limiter.limit("20/hour")
def activate_fia_edition(request: Request, edition_id: str, body: ActivateEditionRequest = ActivateEditionRequest()):
    """Make this edition the one live screening uses. Activating a past edition is how you roll back."""
    result = fia_redbook.activate_edition(edition_id, confirm_reviewed=body.confirm_reviewed, note=body.note)
    _log_admin(request, "fia_edition_activated", target=edition_id, detail=body.note)
    return result


@app.post("/api/admin/fia-redbook/check", dependencies=[Depends(require_api_key)])
@limiter.limit("10/hour")
def check_fia_website(request: Request):
    """
    Looks for a new Red Book on fia.gov.pk and stages it if found — does NOT
    activate it automatically (unlike the scheduled combined refresh below,
    which will auto-activate a scraped edition if it passes basic sanity
    checks). Use this for an on-demand "check now" button; review and
    activate the result from the editions list.
    """
    result = fia_redbook.refresh_cache(auto_activate=False)
    _log_admin(request, "fia_check_website", detail=result.get("status"))
    return result


# --- legacy endpoints, kept for any client still calling them directly ----

@app.post("/api/admin/fia-redbook/upload", dependencies=[Depends(require_api_key)])
@limiter.limit("10/hour")
async def upload_fia_redbook(request: Request, file: UploadFile = File(...)):
    """
    Legacy one-call swap: stage the PDF and activate it immediately, no
    separate review step. Prefer POST .../editions + .../activate, which lets
    you review parsed entries before they go live. The previous live edition
    is retained, not deleted.
    """
    contents = await _read_upload(file)
    result = fia_redbook.ingest_uploaded_pdf(contents, original_filename=file.filename)
    _log_admin(request, "fia_edition_activated", target=result["edition_id"], detail="via legacy upload endpoint")
    return result


@app.get("/api/admin/fia-redbook/status", dependencies=[Depends(require_api_key)])
def fia_redbook_status(request: Request):
    """
    What Red Book edition is currently loaded. Kept for back-compat; the FIA
    Red Book tab uses GET .../editions instead, which also shows staged and
    archived editions.
    """
    return fia_redbook.get_status()


@app.post("/api/admin/refresh", dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
def refresh_caches(request: Request):
    """
    Combined refresh of every source with a free, automated feed (UNSC,
    OFAC, UKSL, and the FIA scraper). Prefer the dedicated per-source
    endpoints (/api/admin/refresh-unsc, -ofac, -uksl) + the FIA Red Book tab
    if you're managing editions individually.

    Each source is refreshed independently and a failure in one does NOT
    stop the others from running. Check each entry's "error" key; a missing
    key means that source refreshed successfully. A scraped Red Book is only
    auto-activated if it passes basic sanity checks (see
    FIA_AUTO_ACTIVATE_MIN_RATIO in app/config.py); otherwise it's left staged
    for review, same as the dedicated "check FIA website" button.
    """
    results = {}
    for name, refresh_fn in (
        ("unsc", unsc.refresh_cache),
        ("ofac", ofac.refresh_cache),
        ("uksl", uksl.refresh_cache),
        ("fia_redbook", fia_redbook.refresh_cache),
    ):
        try:
            results[name] = refresh_fn()
        except Exception as e:
            from app.errors import describe_exception
            code, message, hint = describe_exception(e)
            logger.exception("Combined refresh: %s failed", name)
            results[name] = {"error": message, "error_code": code, "hint": hint}
    _log_admin(request, "combined_refresh",
              detail=", ".join(f"{k}={'ok' if 'error' not in v else 'failed'}" for k, v in results.items()))
    return results


@app.get("/api/health")
def health():
    # Intentionally unauthenticated — Render/uptime monitors need to hit this.
    return {"status": "ok"}
