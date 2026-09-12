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
    POST /api/admin/refresh-unsc    -> refresh the UNSC cache
    POST /api/admin/fia-redbook/upload -> upload a Red Book PDF manually (replaces the current edition; old one is archived, not lost)
    GET  /api/admin/fia-redbook/status -> what edition is currently loaded, and when
    POST /api/admin/refresh         -> legacy combined refresh (UNSC + FIA scrape)
"""

from pathlib import Path
from datetime import datetime, timezone

import os

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app import database as db
from app.config import SCREENSHOT_DIR, MATCH_THRESHOLD, REVIEW_THRESHOLD
from app.schemas import ScreenRequest, ScreenResponse, ScreeningResultOut, ApplicantSummary, NearMissEntry
from app.screening import unsc, fia_redbook, adverse_media
from app import evidence
from app.auth import require_api_key

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Account Screening API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Comma-separated list in env, e.g.:
#   ALLOWED_ORIGINS=https://your-app.vercel.app,https://your-app-git-main.vercel.app
_default_origins = "http://localhost:5173"
allowed_origins = os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)


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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Don't leak internal details (stack traces, file paths, library errors)
    # to clients — log server-side, return a generic message externally.
    print(f"Unhandled error on {request.url.path}: {exc!r}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.on_event("startup")
def startup():
    db.init_db()
    if not os.environ.get("API_KEY"):
        print(
            "\n*** WARNING: API_KEY is not set. Every protected endpoint will "
            "return 503 until it is configured. Do not deploy like this. ***\n"
        )


def _status_for(result: dict) -> str:
    """
    Turns a source-check result dict into HIT / REVIEW / CLEAR / NOT_CONFIGURED.

    Two rules that override a bare score comparison:
      - A source that couldn't actually run (cache empty, no API key) must
        NOT be reported as CLEAR — that would silently hide the fact that
        the check never happened.
      - An exact CNIC match is direct identity evidence, not a fuzzy
        inference, so it always resolves to HIT regardless of the name
        score — a shared 13-digit national ID number should never be
        outvoted by a mediocre fuzzy name score.
    """
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


@app.post("/api/screen", response_model=ScreenResponse, dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def screen_applicant(request: Request, req: ScreenRequest):
    now = datetime.now(timezone.utc).isoformat()
    applicant_id = db.insert_applicant(req.full_name, req.cnic, req.father_name, now, "PENDING")

    results_out = []
    statuses = []

    # --- UNSC ---
    unsc_result = unsc.check(req.full_name, threshold=REVIEW_THRESHOLD)
    status = _status_for(unsc_result)
    statuses.append(status)
    _log_near_miss_if_any(applicant_id, "UNSC", unsc_result, now)
    if status == "HIT":
        result_id_placeholder = db.insert_result(
            applicant_id, "UNSC", unsc_result["matched_entry"], unsc_result["score"],
            status, unsc_result["detail"], None, now,
            cnic_match=unsc_result.get("cnic_match", False),
        )
        pdf_path = evidence.generate_evidence_pdf(
            req.full_name, req.cnic, "UNSC Consolidated Sanctions List",
            unsc_result["matched_entry"], unsc_result["score"], unsc_result["source_url"],
            image_path=None,  # UNSC is a static data feed, not a webpage — no screenshot
            result_id=result_id_placeholder,
            cnic_match=unsc_result.get("cnic_match", False),
            breakdown=unsc_result.get("breakdown"),
        )
        evidence_file = pdf_path.name
        with db.get_conn() as conn:
            conn.execute("UPDATE screening_results SET evidence_file = ? WHERE id = ?",
                         (evidence_file, result_id_placeholder))
            conn.commit()
        results_out.append(ScreeningResultOut(
            id=result_id_placeholder, source="UNSC", matched_entry=unsc_result["matched_entry"],
            score=unsc_result["score"], status=status, detail=unsc_result["detail"],
            evidence_file=evidence_file, checked_at=now,
            cnic_match=unsc_result.get("cnic_match", False), near_miss=unsc_result.get("near_miss", False),
        ))
    else:
        rid = db.insert_result(applicant_id, "UNSC", unsc_result["matched_entry"], unsc_result["score"],
                                status, unsc_result["detail"], None, now,
                                cnic_match=unsc_result.get("cnic_match", False),
                                near_miss=unsc_result.get("near_miss", False))
        results_out.append(ScreeningResultOut(
            id=rid, source="UNSC", matched_entry=unsc_result["matched_entry"],
            score=unsc_result["score"], status=status, detail=unsc_result["detail"],
            evidence_file=None, checked_at=now,
            cnic_match=unsc_result.get("cnic_match", False), near_miss=unsc_result.get("near_miss", False),
        ))

    # --- FIA Red Book ---
    fia_result = fia_redbook.check(req.full_name, applicant_cnic=req.cnic, threshold=REVIEW_THRESHOLD)
    status = _status_for(fia_result)
    statuses.append(status)
    _log_near_miss_if_any(applicant_id, "FIA_REDBOOK", fia_result, now)
    if status == "HIT":
        result_id_placeholder = db.insert_result(
            applicant_id, "FIA_REDBOOK", fia_result["matched_entry"], fia_result["score"],
            status, fia_result["detail"], None, now,
            cnic_match=fia_result.get("cnic_match", False),
        )
        image_path = None
        if fia_result.get("page_number") is not None:
            image_path = SCREENSHOT_DIR / f"fia_page_{result_id_placeholder}.png"
            fia_redbook.render_matched_page(fia_result["page_number"], image_path)
        pdf_path = evidence.generate_evidence_pdf(
            req.full_name, req.cnic, "FIA Red Book",
            fia_result["matched_entry"], fia_result["score"], fia_result["source_url"],
            image_path=image_path, result_id=result_id_placeholder,
            cnic_match=fia_result.get("cnic_match", False),
            breakdown=fia_result.get("breakdown"),
        )
        evidence_file = pdf_path.name
        with db.get_conn() as conn:
            conn.execute("UPDATE screening_results SET evidence_file = ? WHERE id = ?",
                         (evidence_file, result_id_placeholder))
            conn.commit()
        results_out.append(ScreeningResultOut(
            id=result_id_placeholder, source="FIA_REDBOOK", matched_entry=fia_result["matched_entry"],
            score=fia_result["score"], status=status, detail=fia_result["detail"],
            evidence_file=evidence_file, checked_at=now,
            cnic_match=fia_result.get("cnic_match", False), near_miss=fia_result.get("near_miss", False),
        ))
    else:
        rid = db.insert_result(applicant_id, "FIA_REDBOOK", fia_result["matched_entry"], fia_result["score"],
                                status, fia_result["detail"], None, now,
                                cnic_match=fia_result.get("cnic_match", False),
                                near_miss=fia_result.get("near_miss", False))
        results_out.append(ScreeningResultOut(
            id=rid, source="FIA_REDBOOK", matched_entry=fia_result["matched_entry"],
            score=fia_result["score"], status=status, detail=fia_result["detail"],
            evidence_file=None, checked_at=now,
            cnic_match=fia_result.get("cnic_match", False), near_miss=fia_result.get("near_miss", False),
        ))

    # --- Adverse Media ---
    media_screenshot_path = SCREENSHOT_DIR / f"media_{applicant_id}.png"
    media_result = adverse_media.check(req.full_name, screenshot_out_path=media_screenshot_path)
    media_status = media_result.get("status") or _status_for(
        {"score": media_result.get("score"), "available": media_result.get("available", True)}
    )
    statuses.append(media_status)
    if media_status in ("HIT", "REVIEW") and media_result.get("screenshot_path"):
        result_id_placeholder = db.insert_result(
            applicant_id, "ADVERSE_MEDIA", media_result.get("matched_entry"), media_result.get("score"),
            media_status, media_result["detail"], None, now,
        )
        pdf_path = evidence.generate_evidence_pdf(
            req.full_name, req.cnic, "Adverse Media Search",
            media_result.get("matched_entry") or "See screenshot — raw search, not a confirmed match",
            media_result.get("score") or 0, media_result.get("source_url"),
            image_path=Path(media_result["screenshot_path"]), result_id=result_id_placeholder,
        )
        evidence_file = pdf_path.name
        with db.get_conn() as conn:
            conn.execute("UPDATE screening_results SET evidence_file = ? WHERE id = ?",
                         (evidence_file, result_id_placeholder))
            conn.commit()
        results_out.append(ScreeningResultOut(
            id=result_id_placeholder, source="ADVERSE_MEDIA", matched_entry=media_result.get("matched_entry"),
            score=media_result.get("score"), status=media_status, detail=media_result["detail"],
            evidence_file=evidence_file, checked_at=now,
        ))
    else:
        rid = db.insert_result(applicant_id, "ADVERSE_MEDIA", media_result.get("matched_entry"),
                                media_result.get("score"), media_status, media_result["detail"], None, now)
        results_out.append(ScreeningResultOut(
            id=rid, source="ADVERSE_MEDIA", matched_entry=media_result.get("matched_entry"),
            score=media_result.get("score"), status=media_status, detail=media_result["detail"],
            evidence_file=None, checked_at=now,
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
    if not result or not result.get("evidence_file"):
        raise HTTPException(404, "No evidence file for this result")
    path = evidence.EVIDENCE_DIR / result["evidence_file"]
    if not path.exists():
        raise HTTPException(404, "Evidence file missing on disk")
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


MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB — Red Book PDFs are small; this is a generous ceiling


@app.post("/api/admin/refresh-unsc", dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
def refresh_unsc_cache(request: Request):
    """Refresh the UNSC sanctions list cache. Safe to call on a daily schedule."""
    return unsc.refresh_cache()


@app.post("/api/admin/fia-redbook/upload", dependencies=[Depends(require_api_key)])
@limiter.limit("10/hour")
async def upload_fia_redbook(request: Request, file: UploadFile = File(...)):
    """
    Upload the FIA Red Book PDF manually (downloaded from fia.gov.pk and
    reviewed) instead of relying on the scraper. Replaces the cached edition.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file (.pdf extension required)")

    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large — max {MAX_UPLOAD_BYTES // (1024*1024)}MB")
    if not contents.startswith(b"%PDF-"):
        # Trust file contents, not the filename or declared content-type —
        # both are attacker-controlled and easy to spoof.
        raise HTTPException(400, "File does not look like a valid PDF")

    result = fia_redbook.ingest_uploaded_pdf(contents, original_filename=file.filename)
    return result


@app.get("/api/admin/fia-redbook/status", dependencies=[Depends(require_api_key)])
def fia_redbook_status(request: Request):
    """
    What Red Book edition is currently loaded (filename, when it was
    loaded, how many names, how many older editions are archived) — check
    this to confirm an upload actually took, without re-uploading anything.
    """
    return fia_redbook.get_status()


@app.post("/api/admin/refresh", dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
def refresh_caches(request: Request):
    """
    Legacy combined refresh — attempts the FIA scraper too. Prefer
    /api/admin/refresh-unsc + /api/admin/fia-redbook/upload if you're
    managing the Red Book edition manually.
    """
    unsc_status = unsc.refresh_cache()
    fia_status = fia_redbook.refresh_cache()
    return {"unsc": unsc_status, "fia_redbook": fia_status}


@app.get("/api/health")
def health():
    # Intentionally unauthenticated — Render/uptime monitors need to hit this.
    return {"status": "ok"}
