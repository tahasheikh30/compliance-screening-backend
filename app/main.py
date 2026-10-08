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
    GET  /api/applicants                  your past screenings (admins: everyone's; ?mine=true for their own)
    GET  /api/applicants/{id}             full result for one screening
    GET  /api/applicants/{id}/evidence    evidence PDF of a screening that found something
    GET  /api/evidence/{result_id}        same PDF, addressed by a result row id
    GET  /api/admin/lists                 which lists are cached in memory right now
    GET  /api/admin/nacta                 which NACTA file is loaded and how old it is
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
import os
import uuid
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
from app import evidence
from app import config as app_config
from app import auth
from app import monitoring
from app.auth import AuthUser, require_admin, require_admin_or_service_key, require_approved
from app.config import FIA_REQUIRED, NACTA_REQUIRED, PRELOAD_LISTS
from app.errors import AppError, configure_logging, install as install_error_handlers, request_context, logger
from app.schemas import (AlertDecisionIn, AlertOut, ApplicantSummary, AuditEntryOut, AuditPageOut, AuditVerifyOut, MeOut,
                         MonitoringIn, MonitoringOut, MonitoringStatusOut, SourceMonitoringOut, ScreenRequest, ScreenResponse, ScreeningResultOut, UserOut,
                         UserRoleIn, UserStatusIn)
from app.screening import engine, loader, nacta_store, parsers

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
    if PRELOAD_LISTS and loader.start_background_refresh():
        logger.info("Loading the screening lists in the background and keeping them fresh")
    if monitoring.start():
        logger.info("Continuous monitoring is on: monitored applicants are re-screened when a list changes "
                    "(checked every %d s)", int(app_config.MONITOR_INTERVAL_SECONDS))



def _audit(request: Request, user: AuthUser, action: str, target_type: str | None = None, target_id=None,
           detail: dict | None = None) -> None:
    """
    Record who did what (ids and outcomes only, never an applicant's name or CNIC). A failure to write the
    entry is logged loudly but never turns a screening into an error for the analyst.
    """
    try:
        db.audit(action, actor_id=user.id, actor_email=user.email, via=user.via, target_type=target_type,
                 target_id=target_id, detail=detail, request_id=getattr(request.state, "request_id", None),
                 ip=request.client.host if request.client else None)
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
def screen_applicant(request: Request, req: ScreenRequest, user: AuthUser = Depends(require_approved)):
    now = datetime.now(timezone.utc)
    threshold = engine.resolve_threshold(req.threshold)
    try:
        engine.check_screenable(req.full_name)   # the request model already checked; this is the safety net
    except engine.UnscreenableName as exc:
        raise AppError(422, "NAME_NOT_SCREENABLE", str(exc), "Retype the name in Latin letters.") from None
    applicant_id = db.insert_applicant(req.full_name, req.cnic, req.father_name, now.isoformat(), "PENDING",
                                       dob=req.dob, nationality=req.nationality, threshold=threshold,
                                       user_id=user.id)
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

    overall = engine.overall_status(statuses, FIA_REQUIRED, NACTA_REQUIRED)

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
        _audit(request, user, "monitoring.enrol", "applicant", applicant_id)
    _audit(request, user, "screening.run", "applicant", applicant_id,
           {"overall_status": overall, "threshold": threshold, "sanctions_hits": result["sanctions_hit_count"],
            "media_hits": result["media_hit_count"], "evidence": bool(evidence_pdf)})
    results_out = [
        ScreeningResultOut(**r) for r in db.get_results_for_applicant(applicant_id)
    ]
    return ScreenResponse(
        applicant_id=applicant_id, full_name=req.full_name, overall_status=overall, results=results_out,
        case_ref=case_ref, threshold=threshold, records_screened=result["total_records"],
        sanctions_hit_count=result["sanctions_hit_count"], media_hit_count=result["media_hit_count"],
        monitored=req.monitor,
    )


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
def list_users(request: Request, status: Literal["pending", "approved", "rejected"] | None = None,
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
    """Approve or reject a user."""
    return _change_user(request, user_id, user, status=body.status)


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
def health(deep: bool = False):
    # unauthenticated on purpose: uptime monitors need to reach it. ?deep=true also checks the database.
    if deep:
        try:
            db.ping()
        except Exception:
            logger.exception("Health check: the database is not reachable")
            raise AppError(503, "DATABASE_UNAVAILABLE", "The database cannot be reached right now.") from None
    return {"status": "ok"}
