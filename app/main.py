"""
FastAPI backend for the applicant screening tool.

The screening itself is the n8n "Applicant Screening Engine" workflow, ported to
Python (see app/screening/): the applicant is checked against the UN Security
Council, OFAC (SDN and Consolidated), UK (FCDO), FIA Red Book and NACTA lists, all
downloaded live from the publishers, plus an open news search. A potential match
produces an evidence PDF. No API keys or third party services are needed for that.

Everything is stored in Supabase (Postgres): users, each person's screening history,
the evidence PDFs and the NACTA list. People sign in with Supabase Auth, are approved
by an admin, and see only their own screenings (admins see everyone's). See README.

Run:
    uvicorn app.main:app --reload --port 8000

Every endpoint except /api/health needs the app's key ("X-API-Key: <APP_API_KEY>", sent by the frontend)
AND a signed in person ("Authorization: Bearer <access token>"). The NACTA upload also accepts the secret
API_KEY on its own, for the scheduled workflow.

Endpoints:
    GET  /api/me                          who you are and whether you are approved (works while pending)
    POST /api/screen                      run a screening for one applicant (rate limited)
    POST /api/batch                       screen a whole file (Excel, CSV or Word table) in the background
    GET  /api/batches/{id}                progress and per-row results; /cancel stops it; /results.xlsx and
                                          /evidence.zip download the outcome (a batch is private to its uploader)
    GET  /api/applicants                  your past screenings (admins: everyone's; ?mine=true for their own)
    GET  /api/applicants/{id}             full result for one screening
    GET  /api/applicants/{id}/evidence    evidence PDF of a screening that found something
    GET  /api/evidence/{result_id}        same PDF, addressed by a result row id
    GET  /api/admin/lists                 which lists are cached in memory right now
    GET  /api/admin/nacta                 which NACTA file is loaded and how old it is
    GET  /api/admin/pep                   which PEP data is loaded (Wikidata copy and the admin's own list)
    POST /api/admin/pep                   (admin) upload the PEP list (CSV, JSON or XML); DELETE removes it
    POST /api/admin/pep/refresh           (admin) fetch the Wikidata copy now, in the background
    POST /api/admin/refresh               (admin) drop the in-memory list cache and reload every list
    POST /api/admin/nacta                 (admin, or the secret API_KEY) upload the NACTA CSV or JSON export
    GET  /api/admin/users                 (admin) people who signed up, ?status=pending to see who is waiting
    POST /api/admin/users/{id}/status     (admin) approve or reject someone
    POST /api/admin/users/{id}/role       (admin) make someone an admin, or a normal user again
    POST /api/applicants/{id}/monitoring  enrol a screened person in continuous monitoring (or stop)
    GET  /api/monitoring/alerts           new potential matches found by re-screening; /decision records the outcome
    GET  /api/monitoring/status           monitoring state: people watched, open alerts, last check per list
                                          (monitoring is private to the analyst who ran the screening, admins included)
    GET  /api/admin/users/{id}/applicants (admin) one user's screening history
    POST /api/admin/monitoring/run        (admin) run the check now (?force=true re-screens everyone)
    GET  /api/admin/audit                 (admin) who did what, paged; /api/admin/audit/verify checks the hash chain
    GET  /api/health                      unauthenticated liveness check (?deep=true also checks the database)
"""

import hashlib
import io
import os
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app import database as db
from app.loadshed import screen_lane
from app import evidence
from app import config as app_config
from app import auth
from app import batch as batch_runner
from app import supabase_admin
from app import batch_files
from app import monitoring
from app.auth import AuthUser, require_admin, require_admin_or_service_key, require_approved
from app.config import FIA_REQUIRED, NACTA_REQUIRED, PEP_REQUIRED, PRELOAD_LISTS
from app.errors import AppError, configure_logging, install as install_error_handlers, request_context, logger
from app.schemas import (AlertDecisionIn, AlertOut, ApplicantSummary, AuditEntryOut, AuditPageOut, AuditVerifyOut,
                         BatchOut, BatchRowOut, MeOut,
                         MonitoringIn, MonitoringOut, MonitoringStatusOut, SourceMonitoringOut, ScreenRequest, ScreenResponse, ScreeningResultOut, UserDeletedOut, UserOut,
                         UserRoleIn, UserStatusIn)
from app.screening import engine, loader, nacta_store, parsers, pep

configure_logging()

from slowapi import Limiter  # noqa: E402  (after logging config, before app)
from slowapi.util import get_remote_address  # noqa: E402

def _rate_key(request: Request) -> str:
    # one allowance per signed in person (set by app.auth); a request that never got that far is counted by address
    return getattr(request.state, "rate_key", None) or get_remote_address(request)


limiter = Limiter(key_func=_rate_key)



@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    try:
        yield
    finally:
        batch_runner.shutdown()
        monitoring.stop()
        loader.stop_background_refresh()
        db.close_pool()


# The interactive docs list every route to anyone who asks: off unless ENABLE_DOCS=true (local development).
app = FastAPI(title="Applicant Screening API", lifespan=lifespan,
              docs_url="/docs" if app_config.ENABLE_DOCS else None,
              redoc_url="/redoc" if app_config.ENABLE_DOCS else None,
              openapi_url="/openapi.json" if app_config.ENABLE_DOCS else None)
app.state.limiter = limiter
install_error_handlers(app)  # handlers for AppError, HTTPException, 422, 429 and a catch-all

# Comma-separated list, e.g. ALLOWED_ORIGINS=https://your-app.vercel.app
allowed_origins = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
                   if o.strip()]
if "*" in allowed_origins:
    # a wildcard would let any website's script call this API with a signed in person's token
    logger.warning("ALLOWED_ORIGINS contains '*', which is ignored. List your frontend's exact origin.")
    allowed_origins = [o for o in allowed_origins if o != "*"]

# Middleware order matters: Starlette's add_middleware() PREPENDS, so the last
# one registered is outermost. request_context must be registered first (innermost)
# so that the structured 500 it produces still passes back through CORS and the
# security headers. Reversing this makes browsers report an opaque "Failed to fetch"
# instead of the real error (see tests/test_api.py::test_500_response_still_has_cors_header).
app.middleware("http")(request_context)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    # this is a JSON/PDF API: it never needs to load or run anything, so forbid all of it
    h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    # applicant PII: never cache API responses
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(GZipMiddleware, minimum_size=1024)   # screening results with many matches are large JSON
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)


def _startup():
    try:
        db.init_db()
    except Exception:
        # keep serving: /api/health still answers and every other request explains the problem with a 503
        logger.exception("The database is not reachable at startup. Check DATABASE_URL.")
    if not app_config.SUPABASE_URL and not app_config.SUPABASE_JWT_SECRET:
        logger.warning("Neither SUPABASE_URL nor SUPABASE_JWT_SECRET is set, so no sign in can be verified and every "
                       "user request will be refused. Do not deploy like this.")
    if app_config.REQUIRE_APP_KEY and not app_config.APP_API_KEY:
        logger.warning("APP_API_KEY is not set, so every request from the app will be refused. Set it (and the same "
                       "value as VITE_API_KEY on the frontend), or set REQUIRE_APP_KEY=false for local development only.")
    if not app_config.REQUIRE_APP_KEY:
        logger.warning("REQUIRE_APP_KEY is off: requests are not checked for the app's key. Local development only.")
    if auth.API_KEY and app_config.APP_API_KEY and auth.API_KEY == app_config.APP_API_KEY:
        logger.warning("API_KEY and APP_API_KEY are the same value. The app key is built into the frontend, so anyone "
                       "who opens the app could replace the NACTA list. Give them different values.")
    if not auth.API_KEY:
        logger.info("API_KEY is not set: the scheduled NACTA upload by access key is disabled.")
    batch_runner.startup()
    _reap_batches()
    if PRELOAD_LISTS and loader.start_background_refresh():
        logger.info("Loading the screening lists in the background and keeping them fresh")
    if monitoring.start():
        logger.info("Continuous monitoring is on: monitored applicants are re-screened when a list changes "
                    "(checked every %d s)", int(app_config.MONITOR_INTERVAL_SECONDS))



def _audit(request: Request | None, user: AuthUser, action: str, target_type: str | None = None, target_id=None,
           detail: dict | None = None) -> None:
    """
    Record who did what (ids and outcomes only, never an applicant's name or CNIC). A failure to write the
    entry is logged loudly but never turns a screening into an error for the analyst.
    """
    try:
        db.audit(action, actor_id=user.id, actor_email=user.email, via=user.via, target_type=target_type,
                 target_id=target_id, detail=detail, request_id=getattr(request.state, "request_id", None) if request else None,
                 ip=request.client.host if request and request.client else None)
    except Exception:
        logger.exception("AUDIT WRITE FAILED for %s (target %s)", action, target_id)


def _case_ref(applicant_id: int, when: datetime) -> str:
    return f"CS-{when:%Y%m%d}-{applicant_id:05d}"


def _scope(user: AuthUser, mine: bool = False):
    """Whose screenings this user may see: everyone's (None) for an admin, otherwise only their own."""
    return None if (user.is_admin and not mine) else user.id


@app.get("/api/me", response_model=MeOut)
@limiter.limit("60/minute")
def me(request: Request, user: AuthUser = Depends(auth.authenticate)):
    """Who you are and whether your account is approved yet. Works for a user who is still pending."""
    return MeOut(id=user.id, email=user.email, role=user.role, status=user.status)


@app.post("/api/screen", response_model=ScreenResponse)
@limiter.limit("10/minute")
async def screen_applicant(request: Request, req: ScreenRequest, user: AuthUser = Depends(require_approved)):
    # a dedicated, bounded lane (see loadshed.py): a burst of screenings cannot starve the rest of the API
    return await screen_lane.run(_screen_one, req, user, request)


def _screen_one(req: ScreenRequest, user: AuthUser, request: Request | None = None,
                batch_id: int | None = None) -> ScreenResponse:
    """
    One screening, saved to the person's history. Used by the screening route and, row by row, by a batch
    (which has no request: it runs in a background thread, so the audit entry carries the batch id instead).
    """
    now = datetime.now(timezone.utc)
    threshold = engine.resolve_threshold(req.threshold)
    try:
        engine.check_screenable(req.full_name)   # the request model already checked; this is the safety net
    except engine.UnscreenableName as exc:
        raise AppError(422, "NAME_NOT_SCREENABLE", str(exc), "Retype the name in Latin letters.") from None
    applicant_id = db.insert_applicant(req.full_name, req.cnic, req.father_name, now.isoformat(), "PENDING",
                                       dob=req.dob, nationality=req.nationality, threshold=threshold,
                                       user_id=user.id, province=req.province)
    case_ref = _case_ref(applicant_id, now)

    result = engine.screen(req.full_name, req.dob or "", req.nationality or "", threshold,
                           cnic=req.cnic or "", father_name=req.father_name or "",
                           province=req.province or "")

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

    overall = engine.overall_status(statuses, FIA_REQUIRED, NACTA_REQUIRED, PEP_REQUIRED)

    # One evidence PDF per screening, kept in the database. If it cannot be made the finding is still saved.
    evidence_pdf, evidence_failed = None, False
    if result["hit"]:
        try:
            evidence_pdf = evidence.generate_evidence_pdf(result, case_ref)
        except Exception:
            logger.exception("Evidence PDF generation failed for applicant %s", applicant_id)
            evidence_failed = True

    # One transaction for all result rows, the final status and the PDF. The PENDING row inserted above
    # stays as the record of the attempt if the screening itself fails.
    db.save_screening(applicant_id, overall, result["total_records"], rows,
                      evidence=evidence_pdf, evidence_failed=evidence_failed)

    if req.monitor:
        db.set_monitoring(applicant_id, True)
        _audit(request, user, "monitoring.enrol", "applicant", applicant_id,
               {"batch_id": batch_id} if batch_id is not None else None)
    _audit(request, user, "screening.run", "applicant", applicant_id,
           {"overall_status": overall, "threshold": threshold, "sanctions_hits": result["sanctions_hit_count"],
            "media_hits": result["media_hit_count"], "pep_hits": result["pep_hit_count"], "evidence": bool(evidence_pdf),
            **({"batch_id": batch_id} if batch_id is not None else {})})
    results_out = [
        ScreeningResultOut(**r) for r in db.get_results_for_applicant(applicant_id)
    ]
    return ScreenResponse(
        applicant_id=applicant_id, full_name=req.full_name, overall_status=overall, results=results_out,
        case_ref=case_ref, threshold=threshold, records_screened=result["total_records"],
        sanctions_hit_count=result["sanctions_hit_count"], media_hit_count=result["media_hit_count"],
        pep_hit_count=result["pep_hit_count"], monitored=req.monitor,
    )


# --------------------------------------------------------------------------
# Batch screening: upload a file of applicants, screen it in the background, poll for progress
# --------------------------------------------------------------------------

def _reap_batches() -> None:
    """
    Mark as interrupted any batch whose server stopped reporting (it died). Safe with several servers: a batch
    that a healthy server is running keeps its heartbeat fresh and is left alone. Called at start-up and
    whenever batches are looked at or started, so a dead server's batch never stays 'running' for ever.
    """
    try:
        n = db.batch_interrupt_stale(app_config.BATCH_STALE_SECONDS)
        if n:
            logger.warning("%d batch(es) lost their server and are marked interrupted", n)
    except Exception:
        logger.exception("Could not mark interrupted batches")


def _batch_out(user_batch: dict, with_rows: bool = True) -> BatchOut:
    rows = db.batch_rows(user_batch["id"])
    out_rows = []
    for r in rows:
        when = db._when(r["submitted_at"]) if r.get("submitted_at") else None
        out_rows.append(BatchRowOut(
            row=r["row_no"], full_name=r["full_name"], state=r["state"], error=r.get("error"),
            applicant_id=r["applicant_id"], overall_status=r.get("overall_status"),
            sanctions=r["sanctions"] if r["state"] == "screened" else None,
            news=r["news"] if r["state"] == "screened" else None,
            pep=r["pep"] if r["state"] == "screened" else None,
            case_ref=_case_ref(r["applicant_id"], when) if r["state"] == "screened" and when else None,
            dob=r.get("dob"), nationality=r.get("nationality")))
    counts = {"screened": 0, "invalid": 0, "failed": 0, "pending": 0,
              "ESCALATE_TO_COMPLIANCE": 0, "MANUAL_REVIEW": 0, "AUTO_CLEAR": 0}
    for r in out_rows:
        counts[r.state] += 1
        if r.state == "screened" and r.overall_status in counts:
            counts[r.overall_status] += 1
    return BatchOut(
        id=user_batch["id"], filename=user_batch["filename"], status=user_batch["status"], total=user_batch["total"],
        done=user_batch["total"] - counts["pending"], threshold=user_batch["threshold"],
        monitor=bool(user_batch["monitor"]), created_at=user_batch["created_at"],
        finished_at=user_batch.get("finished_at"), counts=counts, rows=out_rows if with_rows else [])


def _own_batch(user: AuthUser, batch_id: int) -> dict:
    # like history: only the person who uploaded a batch can see it, administrators included
    b = db.batch_get(batch_id, user.id)
    if not b:
        raise AppError(404, "BATCH_NOT_FOUND", "No batch with that ID.")
    return b


@app.post("/api/batch", response_model=BatchOut, status_code=202)
@limiter.limit("20/hour")
async def start_batch(request: Request, filename: str = Query("applicants.xlsx", max_length=255),
                      threshold: float | None = None, monitor: bool = False,
                      user: AuthUser = Depends(require_approved)):
    """
    Screen a whole file. Send the Excel, CSV or Word file as the request body (not a multipart form), with the
    file name in ?filename=. Every row is screened in the background as an ordinary screening; poll
    GET /api/batches/{id} for progress. A row that cannot be screened is reported on its own row, it does not
    reject the file.
    """
    name = os.path.basename(filename.replace("\\", "/"))
    if batch_files.extension_of(name) not in batch_files.ALLOWED_EXTENSIONS:
        raise AppError(415, "BATCH_FILE_TYPE", "That file type is not supported.",
                       "Use an Excel file (.xlsx or .xls), a CSV, or a Word file (.docx).")
    data = await request.body()
    if not data:
        raise AppError(400, "BATCH_FILE_EMPTY", "The upload was empty.", "Choose the file again and retry.")
    if len(data) > app_config.BATCH_MAX_FILE_BYTES:
        raise AppError(413, "BATCH_FILE_TOO_LARGE", "That file is too large.",
                       f"The limit is {app_config.BATCH_MAX_FILE_BYTES // (1024 * 1024)} MB. Split it into smaller files.")
    _reap_batches()               # a batch whose server died must not block this person from starting another
    if db.batch_running_count(user.id):
        raise AppError(409, "BATCH_ALREADY_RUNNING", "You already have a batch running.",
                       "Wait for it to finish, or cancel it, before starting another.")
    if not batch_runner.capacity_left():
        raise AppError(429, "BATCH_BUSY", "The server is busy with other batches.", "Try again in a few minutes.")

    thr = engine.resolve_threshold(threshold)
    try:
        parsed = await run_in_threadpool(batch_files.parse_applicants, data, name, app_config.BATCH_MAX_ROWS)
    except batch_files.BatchFileError as exc:
        raise AppError(422, exc.code, exc.message, exc.hint) from None
    prepared = batch_runner.prepare_rows(parsed, thr, monitor)
    usable = [(row, req) for row, req in prepared if req is not None]
    if not usable:
        first = next((row["error"] for row, _ in prepared if row.get("error")), None)
        raise AppError(422, "BATCH_NO_VALID_ROWS", "None of the rows in that file could be screened.",
                       f"First problem: {first}" if first else None)

    try:
        batch_id = db.batch_create(user.id, name[:200], thr, monitor, [row for row, _ in prepared], batch_runner.OWNER)
    except db.BatchAlreadyRunning:
        raise AppError(409, "BATCH_ALREADY_RUNNING", "You already have a batch running.",
                       "Wait for it to finish, or cancel it, before starting another.") from None
    _audit(request, user, "batch.start", "batch", batch_id,
           {"rows": len(prepared), "screenable": len(usable), "threshold": thr, "monitor": monitor,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    def finished(status: str) -> None:
        counts = {}
        for r in db.batch_rows(batch_id):
            key = r["overall_status"] if r["state"] == "screened" else r["state"]
            counts[key] = counts.get(key, 0) + 1
        _audit(None, user, "batch.finish", "batch", batch_id, {"status": status, "outcomes": counts})

    batch_runner.start(batch_id, usable, lambda req: _screen_one(req, user, None, batch_id), finished)
    return _batch_out(db.batch_get(batch_id))


@app.get("/api/batches/{batch_id}", response_model=BatchOut)
@limiter.limit("120/minute")
def get_batch(request: Request, batch_id: int, user: AuthUser = Depends(require_approved)):
    """Progress and per-row results of a batch. Poll this while status is 'running'."""
    _reap_batches()
    return _batch_out(_own_batch(user, batch_id))


@app.post("/api/batches/{batch_id}/cancel", response_model=BatchOut)
@limiter.limit("30/minute")
def cancel_batch(request: Request, batch_id: int, user: AuthUser = Depends(require_approved)):
    """Stop after the row being screened. Rows already screened are kept; the rest are reported as not screened."""
    b = _own_batch(user, batch_id)
    # the request goes in the database, so it works whichever server is running the batch
    if b["status"] == "running" and db.batch_request_cancel(batch_id, user.id):
        _audit(request, user, "batch.cancel", "batch", batch_id)
    return _batch_out(b)


@app.get("/api/batches/{batch_id}/results.xlsx")
@limiter.limit("30/minute")
def download_batch_results(request: Request, batch_id: int, user: AuthUser = Depends(require_approved)):
    """The batch as a spreadsheet: one line per row of the uploaded file, including rows that were not screened."""
    b = _own_batch(user, batch_id)

    def case_ref(r):
        return _case_ref(r["applicant_id"], db._when(r["submitted_at"]))

    content = batch_files.results_workbook(b, db.batch_rows(batch_id), case_ref)
    _audit(request, user, "batch.results.download", "batch", batch_id, {"sha256": hashlib.sha256(content).hexdigest()})
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="screening-results-{batch_id}.xlsx"'})


@app.get("/api/batches/{batch_id}/evidence.zip")
@limiter.limit("10/minute")
def download_batch_evidence(request: Request, batch_id: int, user: AuthUser = Depends(require_approved)):
    """Every evidence PDF the batch produced, with a manifest of their SHA-256 hashes."""
    _own_batch(user, batch_id)
    files = db.batch_evidence(batch_id)
    if not files:
        raise AppError(404, "EVIDENCE_NOT_GENERATED", "This batch has no evidence PDFs.", _NO_EVIDENCE)
    buf = io.BytesIO()
    manifest = ["row,file,sha256"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            safe = "".join(c for c in str(f["filename"]) if c.isalnum() or c in "._-") or "evidence.pdf"
            member = f"row-{f['row_no']}-{safe}"
            z.writestr(member, bytes(f["content"]))
            manifest.append(f"{f['row_no']},{member},{f['sha256']}")
        z.writestr("manifest.csv", "\r\n".join(manifest) + "\r\n")
    _audit(request, user, "batch.evidence.download", "batch", batch_id, {"files": len(files)})
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="evidence-batch-{batch_id}.zip"'})



@app.get("/api/applicants", response_model=list[ApplicantSummary])
@limiter.limit("30/minute")
def list_applicants(request: Request, response: Response, mine: bool = False,
                    limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
                    status: Literal["ESCALATE_TO_COMPLIANCE", "MANUAL_REVIEW", "AUTO_CLEAR", "PENDING"] | None = None,
                    user: AuthUser = Depends(require_approved)):
    """Newest first. `limit` and `offset` page through them; X-Total-Count is how many there are in all."""
    rows, total = db.search_applicants(user_id=_scope(user, mine), status=status, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return rows


@app.get("/api/applicants/{applicant_id}", response_model=ScreenResponse)
@limiter.limit("30/minute")
def get_applicant(request: Request, applicant_id: int, user: AuthUser = Depends(require_approved)):
    # someone else's screening looks exactly like one that does not exist
    applicant = db.get_applicant(applicant_id, _scope(user))
    if not applicant:
        raise HTTPException(404, "Applicant not found")
    _audit(request, user, "screening.view", "applicant", applicant_id)
    results = db.get_results_for_applicant(applicant_id)
    try:
        when = datetime.fromisoformat(applicant["submitted_at"])
    except (TypeError, ValueError):
        when = datetime.now(timezone.utc)
    return ScreenResponse(
        applicant_id=applicant_id, full_name=applicant["full_name"], overall_status=applicant["overall_status"],
        results=[ScreeningResultOut(**r) for r in results],
        case_ref=_case_ref(applicant_id, when), threshold=applicant.get("threshold"),
        records_screened=applicant.get("records_screened"), monitored=bool(applicant.get("monitored")),
    )


_NO_EVIDENCE = ("An evidence PDF is only generated when a screening finds a potential match or an adverse "
                "news article. If it should exist and does not, generation may have failed: check the server log.")


def _send_evidence(request: Request, user: AuthUser, applicant_id: int):
    ev = db.get_evidence(applicant_id)
    if not ev:
        raise AppError(404, "EVIDENCE_NOT_GENERATED", "This screening has no evidence PDF.", _NO_EVIDENCE)
    _audit(request, user, "evidence.download", "applicant", applicant_id, {"sha256": ev["sha256"]})
    safe_name = "".join(c for c in str(ev["filename"]) if c.isalnum() or c in "._-") or "evidence.pdf"
    return Response(content=bytes(ev["content"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}"',
                             "X-Content-SHA256": ev["sha256"]})


@app.get("/api/evidence/{result_id}")
@limiter.limit("30/minute")
def download_evidence(request: Request, result_id: int, user: AuthUser = Depends(require_approved)):
    result = db.get_result(result_id, _scope(user))
    if not result:
        raise AppError(404, "RESULT_NOT_FOUND", "No screening result with that ID.")
    if not result.get("evidence_file"):
        raise AppError(404, "EVIDENCE_NOT_GENERATED", "This result has no evidence PDF.", _NO_EVIDENCE)
    return _send_evidence(request, user, result["applicant_id"])


@app.get("/api/applicants/{applicant_id}/evidence")
@limiter.limit("30/minute")
def download_applicant_evidence(request: Request, applicant_id: int, user: AuthUser = Depends(require_approved)):
    if not db.get_applicant(applicant_id, _scope(user)):
        raise AppError(404, "APPLICANT_NOT_FOUND", "No screening with that ID.")
    return _send_evidence(request, user, applicant_id)


@app.get("/api/admin/lists")
@limiter.limit("30/minute")
def list_cache_status(request: Request, user: AuthUser = Depends(require_approved)):
    """Which lists are held in memory right now, how old they are and how many records they have."""
    # the address each list is fetched from, and the text sample used to diagnose a PDF, are for admins only
    return loader.cache_status(detailed=user.is_admin)


@app.post("/api/admin/refresh")
@limiter.limit("5/hour")
def refresh_lists(request: Request, user: AuthUser = Depends(require_admin)):
    """
    Drop the in-memory cache and download every list again now. Each source is
    reported on its own: a failure in one does not stop the others.
    """
    _audit(request, user, "lists.refresh")
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
        # the host only: the address itself may carry a token or credentials, and every approved user can read this
        "url_host": urlparse(app_config.NACTA_PERSONS_URL).hostname if live else None,
        "filename": (meta or {}).get("filename"),
        "uploaded_at": (meta or {}).get("uploaded_at"),
        "records": (meta or {}).get("records"),
        "age_days": round(age, 1) if age is not None else None,
        "max_age_days": app_config.NACTA_MAX_AGE_DAYS,
        "stale": bool(age is not None and not live and age > app_config.NACTA_MAX_AGE_DAYS),
    }


MAX_PEP_UPLOAD_BYTES = 10 * 1024 * 1024


def _pep_status() -> dict:
    out = {}
    for kind in ("upload", "wikidata"):
        meta = db.pep_meta(kind)
        age = nacta_store.age_days(meta)
        out[kind] = {"loaded": bool(meta), "filename": (meta or {}).get("filename"),
                     "uploaded_at": (meta or {}).get("uploaded_at"), "records": (meta or {}).get("records"),
                     "age_days": round(age, 1) if age is not None else None}
    out["wikidata_enabled"] = app_config.PEP_WIKIDATA_ENABLED
    out["refresh_days"] = app_config.PEP_REFRESH_DAYS
    out["lookback_years"] = app_config.PEP_LOOKBACK_YEARS
    out["required"] = app_config.PEP_REQUIRED
    out["fetch"] = loader.wikidata_fetch_state()
    return out


@app.get("/api/admin/pep")
@limiter.limit("30/minute")
def pep_status(request: Request, user: AuthUser = Depends(require_approved)):
    """Which PEP data is loaded: the Wikidata copy and the administrator's own list."""
    return _pep_status()


def _ingest_pep(data: bytes, filename: str) -> dict:
    try:
        records, info = pep.parse_pep_persons(loader.decode_bytes(data))
    except ValueError as exc:
        raise AppError(422, "PEP_FILE_UNREADABLE", f"The file could not be read: {exc}",
                       "Upload CSV, JSON or XML with a header row that includes a Name column "
                       "(and ideally Position, Level, Province and CNIC).") from None
    if not records:
        raise AppError(422, "PEP_FILE_NO_RECORDS", "The file has no usable records (no names found).",
                       "Check that the first row holds the column headers.")
    db.pep_put("upload", data, os.path.basename(filename or "pep.csv")[:200] or "pep.csv", len(records))
    loader.clear_cache("PEP")
    warnings = []
    if info["no_level"]:
        warnings.append(f"{info['no_level']} people have no level (National or Provincial) and no position that shows one. "
                        "They are still screened as PEPs.")
    if not info["with_cnic"]:
        warnings.append("No usable CNIC numbers were found, so matching will rely on names alone.")
    return {**_pep_status(), "records": len(records), "rows_read": info["rows"], "rows_skipped": info["skipped"],
            "national": info["national"], "provincial": info["provincial"], "warnings": warnings}


@app.post("/api/admin/pep")
@limiter.limit("20/hour")
async def upload_pep(request: Request, filename: str = "pep.csv", user: AuthUser = Depends(require_admin)):
    """
    Load the administrator's own PEP list. Send the file as the request body (not a multipart form), with the
    name in ?filename=. It replaces the previous upload only if it can be read. Wikidata data is not touched.
    """
    data = await request.body()
    if not data:
        raise AppError(400, "PEP_FILE_EMPTY", "The upload was empty.", "Choose the CSV, JSON or XML file and try again.")
    if len(data) > MAX_PEP_UPLOAD_BYTES:
        raise AppError(413, "PEP_FILE_TOO_LARGE", "That file is too large for the PEP list.")
    out = await run_in_threadpool(_ingest_pep, data, filename)
    _audit(request, user, "pep.upload", "pep_list", None,
           {"filename": os.path.basename(filename)[:200], "records": out["records"], "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()})
    return out


@app.post("/api/admin/pep/refresh", status_code=202)
@limiter.limit("10/hour")
def refresh_pep(request: Request, user: AuthUser = Depends(require_admin)):
    """Start fetching the Wikidata copy now (in the background; it can take a minute or two). Poll GET /api/admin/pep."""
    if not app_config.PEP_WIKIDATA_ENABLED:
        raise AppError(409, "PEP_WIKIDATA_OFF", "The Wikidata fetch is switched off on the server.",
                       "Set PEP_WIKIDATA=true on the backend, or upload your own PEP list.")
    started = loader._refresh_wikidata_in_background(force=True)
    _audit(request, user, "pep.refresh", "pep_list", None, {"started": started})
    return {**_pep_status(), "started": started}


@app.delete("/api/admin/pep")
@limiter.limit("20/hour")
def delete_pep_upload(request: Request, user: AuthUser = Depends(require_admin)):
    """Remove the administrator's own PEP list (the Wikidata copy stays)."""
    with db.pool().connection() as conn:
        conn.execute("DELETE FROM pep_files WHERE kind = 'upload'")
    loader.clear_cache("PEP")
    _audit(request, user, "pep.delete", "pep_list", None)
    return _pep_status()


@app.get("/api/admin/nacta")
@limiter.limit("30/minute")
def nacta_status(request: Request, user: AuthUser = Depends(require_approved)):
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
    if not info["has_province"]:
        warnings.append("No province column was found, so a match cannot show whether the province agrees.")
    logger.info("NACTA list uploaded: %s records from %s", len(records), meta["filename"])
    return {**_nacta_status(), "records": len(records), "rows_read": info["rows"], "rows_skipped": info["skipped"],
            "with_cnic": info["with_cnic"], "warnings": warnings}


@app.post("/api/admin/nacta")
@limiter.limit("10/hour")
async def upload_nacta(request: Request, filename: str = "nacta.csv",
                       user: AuthUser = Depends(require_admin_or_service_key)):
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
    out = await run_in_threadpool(_ingest_nacta, data, filename)
    _audit(request, user, "nacta.upload", "nacta_list", None,
           {"filename": os.path.basename(filename)[:200], "records": out["records"], "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()})
    return out


def _user_out(p: dict) -> UserOut:
    return UserOut(id=p["id"], email=p["email"], role=p["role"], status=p["status"],
                   created_at=p["created_at"], decided_at=p.get("decided_at"))


def _change_user(request: Request, user_id, admin: AuthUser, **changes) -> UserOut:
    try:
        profile = db.update_profile(str(user_id), decided_by=admin.id, **changes)
    except db.LastAdminError:
        raise AppError(409, "LAST_ADMIN", "That would leave the tool without an administrator.",
                       "Make someone else an admin first.") from None
    if profile is None:
        raise AppError(404, "USER_NOT_FOUND", "No user with that ID.")
    auth.invalidate_profile(str(user_id))   # takes effect on this server at once
    _audit(request, admin, "user.change", "user", user_id, changes)
    logger.info("User %s changed by %s: %s", user_id, admin.id or admin.via, changes)
    return _user_out(profile)


@app.get("/api/admin/users", response_model=list[UserOut])
@limiter.limit("60/minute")
def list_users(request: Request, status: Literal["pending", "approved", "rejected", "disabled"] | None = None,
               user: AuthUser = Depends(require_admin)):
    """People who signed up, newest first. ?status=pending shows who is waiting for approval."""
    return [_user_out(p) for p in db.list_profiles(status)]


@app.get("/api/admin/users/{user_id}/applicants", response_model=list[ApplicantSummary])
@limiter.limit("60/minute")
def list_user_applicants(request: Request, response: Response, user_id: uuid.UUID,
                         limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
                         user: AuthUser = Depends(require_admin)):
    """One person's screening history, newest first (the People tab). X-Total-Count is how many there are in all."""
    if db.get_profile(str(user_id)) is None:
        raise AppError(404, "USER_NOT_FOUND", "No user with that ID.")
    rows, total = db.search_applicants(user_id=str(user_id), limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    _audit(request, user, "user.history.view", "user", user_id)
    return rows


@app.post("/api/admin/users/{user_id}/status", response_model=UserOut)
@limiter.limit("60/minute")
def set_user_status(request: Request, user_id: uuid.UUID, body: UserStatusIn, user: AuthUser = Depends(require_admin)):
    """Approve, reject, disable (or switch back on) a user. An admin cannot disable their own account."""
    if body.status == "disabled" and str(user_id) == user.id:
        raise AppError(409, "CANNOT_DISABLE_SELF", "You cannot disable your own account.",
                       "Ask another administrator to do it.")
    return _change_user(request, user_id, user, status=body.status)


@app.delete("/api/admin/users/{user_id}", response_model=UserDeletedOut)
@limiter.limit("20/minute")
def delete_user(request: Request, user_id: uuid.UUID, user: AuthUser = Depends(require_admin)):
    """
    Delete a person: their sign in account and their profile. Their past screenings stay in the history (with
    nobody's name on them, visible to administrators only), so the compliance record is never lost.
    The person is disabled first, so they are locked out at once and a failure half way never leaves them with
    access. Refused for yourself, for the last administrator, and when the sign in service is not set up.
    """
    uid = str(user_id)
    if uid == user.id:
        raise AppError(409, "CANNOT_DELETE_SELF", "You cannot delete your own account.",
                       "Ask another administrator to do it.")
    if not supabase_admin.configured():
        raise AppError(503, "USER_DELETE_NOT_CONFIGURED", "Deleting people is not set up on this server.",
                       "Set SUPABASE_SERVICE_ROLE_KEY on the backend, or use Disable instead.")
    try:
        profile = db.update_profile(uid, status="disabled", decided_by=user.id)   # locks them out; keeps the last admin
    except db.LastAdminError:
        raise AppError(409, "LAST_ADMIN", "That would leave the tool without an administrator.",
                       "Make someone else an admin first.") from None
    if profile is None:
        raise AppError(404, "USER_NOT_FOUND", "No user with that ID.")
    auth.invalidate_profile(uid)
    try:
        removed = supabase_admin.delete_auth_user(uid)
    except supabase_admin.AuthServiceError as exc:
        logger.error("Deleting user %s: %s", uid, exc)
        _audit(request, user, "user.delete.failed", "user", uid, {"email": profile["email"]})
        raise AppError(502, "USER_DELETE_FAILED", "The account was disabled but could not be removed.",
                       "The sign in service did not answer. Try Delete again in a moment.") from None
    try:
        db.delete_profile(uid)
    except db.LastAdminError:       # cannot happen once they are disabled; never delete the last admin regardless
        raise AppError(409, "LAST_ADMIN", "That would leave the tool without an administrator.") from None
    auth.invalidate_profile(uid)
    _audit(request, user, "user.delete", "user", uid, {"email": profile["email"], "sign_in_removed": removed})
    logger.info("User %s deleted by %s", uid, user.id or user.via)
    return UserDeletedOut(id=uid, email=profile["email"], sign_in_removed=removed)


@app.post("/api/admin/users/{user_id}/role", response_model=UserOut)
@limiter.limit("60/minute")
def set_user_role(request: Request, user_id: uuid.UUID, body: UserRoleIn, user: AuthUser = Depends(require_admin)):
    """Make a user an admin, or a normal user again."""
    return _change_user(request, user_id, user, role=body.role)


# --------------------------------------------------------------------------
# Continuous monitoring
# --------------------------------------------------------------------------

@app.post("/api/applicants/{applicant_id}/monitoring", response_model=MonitoringOut)
@limiter.limit("30/minute")
def set_applicant_monitoring(request: Request, applicant_id: int, body: MonitoringIn,
                             user: AuthUser = Depends(require_approved)):
    """
    Put a screened person under continuous monitoring (or take them out): they are screened again whenever a
    watch list changes, and any NEW potential match becomes an alert. Enrolling also checks them against the
    current lists straight away, so someone screened weeks ago is not left unchecked until the next list update.
    """
    applicant = db.get_applicant(applicant_id, _scope(user, mine=True))
    if not applicant:
        raise HTTPException(404, "Applicant not found")
    if body.enabled:
        try:
            engine.check_screenable(applicant["full_name"])
        except engine.UnscreenableName as exc:
            raise AppError(422, "NAME_NOT_SCREENABLE", str(exc), "Screen the person again with the name in Latin letters.") from None
    db.set_monitoring(applicant_id, body.enabled, _scope(user, mine=True))
    new_alerts = len(monitoring.check_applicant(applicant_id)) if body.enabled else 0
    _audit(request, user, "monitoring.enrol" if body.enabled else "monitoring.stop", "applicant", applicant_id,
           {"new_alerts": new_alerts} if body.enabled else None)
    row = db.get_applicant(applicant_id) or {}
    return MonitoringOut(applicant_id=applicant_id, monitored=bool(row.get("monitored")),
                         monitored_since=row.get("monitored_since"), last_monitored_at=row.get("last_monitored_at"),
                         new_alerts=new_alerts)


@app.get("/api/monitoring/alerts", response_model=list[AlertOut])
@limiter.limit("60/minute")
def monitoring_alerts(request: Request, response: Response,
                      status: Literal["open", "confirmed", "dismissed"] | None = "open",
                      limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
                      user: AuthUser = Depends(require_approved)):
    """New potential matches found by re-screening, newest first (default: the open ones). X-Total-Count has the total."""
    rows, total = db.list_alerts(_scope(user, mine=True), status, limit, offset)
    response.headers["X-Total-Count"] = str(total)
    return rows


@app.post("/api/monitoring/alerts/{alert_id}/decision", response_model=AlertOut)
@limiter.limit("60/minute")
def decide_monitoring_alert(request: Request, alert_id: int, body: AlertDecisionIn,
                            user: AuthUser = Depends(require_approved)):
    """Record a person's decision on an alert: confirmed (a real match) or dismissed (someone else with the same name)."""
    if not db.get_alert(alert_id, _scope(user, mine=True)):
        raise AppError(404, "ALERT_NOT_FOUND", "No alert with that ID.")
    db.decide_alert(alert_id, body.status, body.note, user.id)
    _audit(request, user, "monitoring.decision", "alert", alert_id, {"status": body.status})
    return db.get_alert(alert_id, _scope(user, mine=True))


@app.get("/api/monitoring/status", response_model=MonitoringStatusOut)
@limiter.limit("60/minute")
def monitoring_status(request: Request, user: AuthUser = Depends(require_approved)):
    """Whether monitoring is on, how many people are watched, how many alerts are open, and when each list was last checked."""
    state = db.monitoring_state()
    return MonitoringStatusOut(
        enabled=app_config.MONITORING_ENABLED, interval_seconds=app_config.MONITOR_INTERVAL_SECONDS,
        **db.monitoring_counts(_scope(user, mine=True)),
        sources=[SourceMonitoringOut(source=k, last_checked_at=(state.get(k) or {}).get("checked_at"),
                                     applicants_checked=(state.get(k) or {}).get("rescreened"),
                                     new_alerts=(state.get(k) or {}).get("new_alerts")) for k in loader.SOURCE_KEYS])


@app.post("/api/admin/monitoring/run")
@limiter.limit("6/hour")
def run_monitoring_now(request: Request, force: bool = False, user: AuthUser = Depends(require_admin)):
    """Run the monitoring check now. `force=true` re-screens every monitored person against every list, changed or not."""
    _audit(request, user, "monitoring.run_requested", detail={"force": force})
    return monitoring.run_once(force=force)


@app.get("/api/admin/audit", response_model=AuditPageOut)
@limiter.limit("30/minute")
def audit_trail(request: Request, response: Response, limit: int = Query(100, ge=1, le=500),
                offset: int = Query(0, ge=0, le=1_000_000), action: str | None = Query(None, max_length=60),
                actor_id: uuid.UUID | None = None, user: AuthUser = Depends(require_admin)):
    """Who did what, newest first. Entries hold ids and outcomes, never applicant names."""
    rows, total = db.audit_list(limit, offset, action, str(actor_id) if actor_id else None)
    return AuditPageOut(total=total, entries=[AuditEntryOut(**r) for r in rows])


@app.get("/api/admin/audit/verify", response_model=AuditVerifyOut)
@limiter.limit("6/hour")
def audit_verify(request: Request, user: AuthUser = Depends(require_admin)):
    """Re-compute the whole hash chain. ok=false means an entry was changed, removed or inserted."""
    result = db.audit_verify()
    if not result["ok"]:
        logger.error("AUDIT CHAIN BROKEN at entry %s", result["first_bad_id"])
    return result


@app.get("/api/health")
async def health(deep: bool = False):
    # unauthenticated on purpose: uptime monitors need to reach it. ?deep=true also checks the database.
    if deep:
        try:
            await run_in_threadpool(db.ping)
        except Exception:
            logger.exception("Health check: the database is not reachable")
            raise AppError(503, "DATABASE_UNAVAILABLE", "The database cannot be reached right now.") from None
    return {"status": "ok"}
