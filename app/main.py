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
    GET  /api/health                      unauthenticated liveness check
"""

import os
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app import database as db
from app import evidence
from app.auth import require_api_key
from app.config import EVIDENCE_DIR, FIA_REQUIRED
from app.errors import AppError, configure_logging, install as install_error_handlers, request_context, logger
from app.schemas import ApplicantSummary, ScreenRequest, ScreenResponse, ScreeningResultOut
from app.screening import engine, loader

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

    result = engine.screen(req.full_name, req.dob or "", req.nationality or "", threshold)

    statuses: dict = {}
    rows: list = []
    for key in engine.SOURCE_ORDER:
        src = result["sources"][key]
        status = engine.source_status(src, FIA_REQUIRED)
        statuses[key] = status
        best = src["matches"][0] if src["matches"] else None
        detail = engine.describe_source(src, status, threshold)
        row_id = db.insert_result(
            applicant_id, key,
            best["primary_name"] if best else (src["articles"][0]["title"] if src["articles"] else None),
            best["score"] if best else None,
            status, detail, None, result["screened_at"],
            list_version=src["list_version"], records_screened=src["records"],
            payload={"matches": src["matches"], "articles": src["articles"]},
        )
        rows.append((row_id, key, src, status, detail, best))

    # One evidence PDF per screening. If it cannot be written the finding is still saved.
    evidence_file = None
    if result["hit"]:
        try:
            pdf_path = evidence.generate_evidence_pdf(result, case_ref)
            evidence_file = pdf_path.name
            db.set_evidence_file_for_applicant(applicant_id, evidence_file)
        except Exception:
            logger.exception("Evidence PDF generation failed for applicant %s", applicant_id)
            note = " [Evidence PDF could not be generated. See the server log for this request ID.]"
            for row_id, _key, _src, status, _detail, _best in rows:
                if status in ("HIT", "REVIEW"):
                    db.append_result_detail(row_id, note)

    overall = engine.overall_status(statuses, FIA_REQUIRED)
    db.update_applicant_status(applicant_id, overall, records_screened=result["total_records"])

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
                    {"records": len(g.records), "lists": [{"list": m["list"], "records": m["records"],
                                                           "published": m.get("published")} for m in g.meta]})
    return out


@app.get("/api/health")
def health():
    # unauthenticated on purpose: uptime monitors need to reach it
    return {"status": "ok"}
