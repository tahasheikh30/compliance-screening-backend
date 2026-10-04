"""
FastAPI backend for the applicant screening tool.

The screening itself is the n8n "Applicant Screening Engine" workflow, ported to
Python (see app/screening/): the applicant is checked against the UN Security
Council, OFAC (SDN and Consolidated), UK (FCDO) and FIA Red Book lists, all
downloaded live from the publishers, plus an open news search. A potential match
produces an evidence PDF. No API keys or third party services are needed.

Run:
    uvicorn app.main:app --reload --port 8000

All endpoints except /api/health require an X-API-Key header matching API_KEY.

Endpoints:
    POST /api/screen                      run a screening for one applicant (rate limited)
    GET  /api/applicants                  list past screenings
    GET  /api/applicants/{id}             full result for one screening
    GET  /api/applicants/{id}/evidence    evidence PDF of a screening that found something
    GET  /api/evidence/{result_id}        same PDF, addressed by a result row id
    GET  /api/admin/lists                 which lists are cached in memory right now
    POST /api/admin/refresh               drop the in-memory list cache and reload every list
    GET  /api/admin/nacta                 which NACTA file is loaded and how old it is
    POST /api/admin/nacta                 upload the NACTA Proscribed Persons CSV or JSON export
    GET  /api/health                      unauthenticated liveness check
"""

import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app import database as db
from app import evidence
from app import config as app_config
from app.auth import require_api_key
from app.config import EVIDENCE_DIR, FIA_REQUIRED, NACTA_REQUIRED, PRELOAD_LISTS
from app.errors import AppError, configure_logging, install as install_error_handlers, request_context, logger
from app.schemas import ApplicantSummary, ScreenRequest, ScreenResponse, ScreeningResultOut
from app.screening import engine, loader, nacta_store, parsers

configure_logging()

from slowapi import Limiter  # noqa: E402  (after logging config, before app)
from slowapi.util import get_remote_address  # noqa: E402

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Applicant Screening API")
app.state.limiter = limiter
install_error_handlers(app)  # handlers for AppError, HTTPException, 422, 429 and a catch-all

# Comma-separated list, e.g. ALLOWED_ORIGINS=https://your-app.vercel.app
allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173").split(",")

# Middleware order matters: Starlette's add_middleware() PREPENDS, so the last
# one registered is outermost. request_context must be registered first (innermost)
# so that the structured 500 it produces still passes back through CORS and the
# security headers. Reversing this makes browsers report an opaque "Failed to fetch"
# instead of the real error (see tests/test_api.py::test_500_response_still_has_cors_header).
app.middleware("http")(request_context)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # applicant PII: never cache API responses
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


@app.on_event("startup")
def startup():
    db.init_db()
    if not os.environ.get("API_KEY"):
        logger.warning("API_KEY is not set. Every protected endpoint will return 503 until it is configured. "
                       "Do not deploy like this.")
    if PRELOAD_LISTS and loader.start_background_refresh():
        logger.info("Loading the screening lists in the background and keeping them fresh")


@app.on_event("shutdown")
def shutdown():
    loader.stop_background_refresh()


def _case_ref(applicant_id: int, when: datetime) -> str:
    return f"CS-{when:%Y%m%d}-{applicant_id:05d}"


@app.post("/api/screen", response_model=ScreenResponse, dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
def screen_applicant(request: Request, req: ScreenRequest):
    now = datetime.now(timezone.utc)
    threshold = engine.resolve_threshold(req.threshold)
    applicant_id = db.insert_applicant(req.full_name, req.cnic, req.father_name, now.isoformat(), "PENDING",
                                       dob=req.dob, nationality=req.nationality, threshold=threshold)
    case_ref = _case_ref(applicant_id, now)

    result = engine.screen(req.full_name, req.dob or "", req.nationality or "", threshold,
                           cnic=req.cnic or "", father_name=req.father_name or "")

    statuses: dict = {}
    rows: list = []
    for key in engine.SOURCE_ORDER:
        src = result["sources"][key]
        status = engine.source_status(src, FIA_REQUIRED)
        statuses[key] = status
        best = src["matches"][0] if src["matches"] else None
        rows.append({
            "source": key,
            "matched_entry": best["primary_name"] if best else (src["articles"][0]["title"] if src["articles"] else None),
            "score": best["score"] if best else None,
            "status": status,
            "detail": engine.describe_source(src, status, threshold),
            "checked_at": result["screened_at"],
            "list_version": src["list_version"],
            "records_screened": src["records"],
            "payload": {"matches": src["matches"], "articles": src["articles"], "match_count": src["match_count"],
                        "lists": src["lists"]},
        })

    # One transaction for all result rows and the final status. The PENDING row inserted above stays
    # as the record of the attempt if the screening itself fails.
    overall = engine.overall_status(statuses, FIA_REQUIRED, NACTA_REQUIRED)
    row_ids = db.save_screening(applicant_id, overall, result["total_records"], rows)

    # One evidence PDF per screening. If it cannot be written the finding is still saved.
    if result["hit"]:
        try:
            pdf_path = evidence.generate_evidence_pdf(result, case_ref)
            db.set_evidence_file_for_applicant(applicant_id, pdf_path.name)
        except Exception:
            logger.exception("Evidence PDF generation failed for applicant %s", applicant_id)
            note = " [Evidence PDF could not be generated. See the server log for this request ID.]"
            for row_id, row in zip(row_ids, rows):
                if row["status"] in ("HIT", "REVIEW"):
                    db.append_result_detail(row_id, note)

    results_out = [
        ScreeningResultOut(**r) for r in db.get_results_for_applicant(applicant_id)
    ]
    return ScreenResponse(
        applicant_id=applicant_id, full_name=req.full_name, overall_status=overall, results=results_out,
        case_ref=case_ref, threshold=threshold, records_screened=result["total_records"],
        sanctions_hit_count=result["sanctions_hit_count"], media_hit_count=result["media_hit_count"],
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
    try:
        when = datetime.fromisoformat(applicant["submitted_at"])
    except (TypeError, ValueError):
        when = datetime.now(timezone.utc)
    return ScreenResponse(
        applicant_id=applicant_id, full_name=applicant["full_name"], overall_status=applicant["overall_status"],
        results=[ScreeningResultOut(**r) for r in results],
        case_ref=_case_ref(applicant_id, when), threshold=applicant.get("threshold"),
        records_screened=applicant.get("records_screened"),
    )


def _send_evidence(filename: str | None):
    if not filename:
        raise AppError(404, "EVIDENCE_NOT_GENERATED", "This result has no evidence PDF.",
                       "An evidence PDF is only generated when a screening finds a potential match or an adverse "
                       "news article. If it should exist and does not, generation may have failed: check the server log.")
    path = EVIDENCE_DIR / filename
    if not path.exists():
        raise AppError(404, "EVIDENCE_FILE_MISSING", "The evidence file is recorded but missing on disk.",
                       "The persistent disk may have been reset since this screening ran (check STORAGE_DIR). "
                       "The finding itself is still in the database.")
    return FileResponse(path, media_type="application/pdf", filename=filename)


@app.get("/api/evidence/{result_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def download_evidence(request: Request, result_id: int):
    result = db.get_result(result_id)
    if not result:
        raise AppError(404, "RESULT_NOT_FOUND", "No screening result with that ID.")
    return _send_evidence(result.get("evidence_file"))


@app.get("/api/applicants/{applicant_id}/evidence", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def download_applicant_evidence(request: Request, applicant_id: int):
    if not db.get_applicant(applicant_id):
        raise AppError(404, "APPLICANT_NOT_FOUND", "No screening with that ID.")
    files = [r["evidence_file"] for r in db.get_results_for_applicant(applicant_id) if r.get("evidence_file")]
    return _send_evidence(files[0] if files else None)


@app.get("/api/admin/lists", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def list_cache_status(request: Request):
    """Which lists are held in memory right now, how old they are and how many records they have."""
    return loader.cache_status()


@app.post("/api/admin/refresh", dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
def refresh_lists(request: Request):
    """
    Drop the in-memory cache and download every list again now. Each source is
    reported on its own: a failure in one does not stop the others.
    """
    loader.clear_cache()
    out = {}
    for key, g in loader.load_groups().items():
        out[key] = ({"error": g.error} if g.error else
                    {"records": len(g.records), "lists": [loader.list_info(m, debug=True) for m in g.meta]})
    return out


MAX_NACTA_UPLOAD_BYTES = 25 * 1024 * 1024
NACTA_MIN_EXPECTED = 500   # the Fourth Schedule has several thousand people; far fewer suggests a partial export


def _nacta_status() -> dict:
    meta = nacta_store.meta()
    live = bool(app_config.NACTA_PERSONS_URL)
    age = nacta_store.age_days(meta)
    return {
        "loaded": bool(meta) or live,
        "source": "url" if live else ("upload" if meta else None),
        "live_copy": bool(meta and meta.get("live")),   # the saved copy came from a live download, not an upload
        "url": app_config.NACTA_PERSONS_URL or None,
        "filename": (meta or {}).get("filename"),
        "uploaded_at": (meta or {}).get("uploaded_at"),
        "records": (meta or {}).get("records"),
        "age_days": round(age, 1) if age is not None else None,
        "max_age_days": app_config.NACTA_MAX_AGE_DAYS,
        "stale": bool(age is not None and not live and age > app_config.NACTA_MAX_AGE_DAYS),
    }


@app.get("/api/admin/nacta", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
def nacta_status(request: Request):
    """Which NACTA list the screening is using, and how old it is."""
    return _nacta_status()


def _ingest_nacta(data: bytes, filename: str) -> dict:
    """Parse, validate and store an uploaded NACTA file. Blocking work, so it runs in a worker thread."""
    try:
        records, info = parsers.parse_nacta_persons(loader.decode_bytes(data))
    except ValueError as exc:
        raise AppError(422, "NACTA_FILE_UNREADABLE", f"The file could not be read: {exc}",
                       "Upload the list as CSV or JSON, with a header row that includes the name column.") from None
    if not records:
        raise AppError(422, "NACTA_FILE_NO_RECORDS", "The file has no usable records (no names found).",
                       "Check that the first row holds the column headers.")
    meta = nacta_store.save(data, filename, len(records))
    loader.clear_cache("NACTA")
    warnings = []
    if len(records) < NACTA_MIN_EXPECTED:
        warnings.append(f"Only {len(records)} people were found. The Fourth Schedule normally has several thousand, "
                        "so this may be a partial export.")
    if not info["with_cnic"]:
        warnings.append("No usable CNIC numbers were found, so matching will rely on names alone.")
    if not info["has_father"]:
        warnings.append("No father's name column was found.")
    logger.info("NACTA list uploaded: %s records from %s", len(records), meta["filename"])
    return {**_nacta_status(), "records": len(records), "rows_read": info["rows"], "rows_skipped": info["skipped"],
            "with_cnic": info["with_cnic"], "warnings": warnings}


@app.post("/api/admin/nacta", dependencies=[Depends(require_api_key)])
@limiter.limit("10/hour")
async def upload_nacta(request: Request, filename: str = "nacta.csv"):
    """
    Load the NACTA Proscribed Persons (Fourth Schedule) list. Send the CSV or JSON export as the
    request body (not a multipart form), with the file name in ?filename=. The file replaces the
    previous one only if it can be read.
    """
    data = await request.body()
    if not data:
        raise AppError(400, "NACTA_FILE_EMPTY", "The upload was empty.", "Choose the exported CSV or JSON file and try again.")
    if len(data) > MAX_NACTA_UPLOAD_BYTES:
        raise AppError(413, "NACTA_FILE_TOO_LARGE", "That file is too large for the NACTA list.",
                       "The Fourth Schedule export is a few megabytes at most. Check you chose the right file.")
    # parsing a few thousand rows is CPU work: keep it off the event loop so other requests are not stalled
    return await run_in_threadpool(_ingest_nacta, data, filename)


@app.get("/api/health")
def health():
    # unauthenticated on purpose: uptime monitors need to reach it
    return {"status": "ok"}
