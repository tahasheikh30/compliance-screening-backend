"""
Structured errors + request tracing.

Goal: when something breaks, the person looking at the screen AND the person
reading the server logs can find each other. Every response carries an
X-Request-ID; every server-side log line for that request carries the same
ID; every error body carries it too. "It failed" becomes "request 3f9a...
failed with FIA_PDF_CORRUPT — here's what to do".

Error body shape (always this shape, for every error, including 404/405/422/
429/500 raised by the framework itself):

    {
      "detail": "<human message>",          # kept as a plain string so any
                                            # older client reading `detail`
                                            # keeps working
      "error": {
        "code": "FIA_PDF_CORRUPT",           # stable, greppable identifier
        "message": "<human message>",
        "hint": "<what to try next>" | null,
        "request_id": "3f9a1c...",
        "fields": [{"field": "full_name", "message": "..."}]   # 422 only
      }
    }

Privacy: applicant names and CNICs are PII. This module never logs request
bodies and never echoes submitted values back (pydantic's default 422 body
includes an `input` key with the raw submitted value — we drop it).
"""

import contextvars
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

logger = logging.getLogger("screening")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging() -> None:
    """Idempotent. Puts the request ID on every log line from our logger."""
    if any(isinstance(f, _RequestIdFilter) for f in logger.filters):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(request_id)s] %(message)s"
    ))
    logger.addFilter(_RequestIdFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class AppError(Exception):
    """
    An error we understand and can explain. Raise this instead of a bare
    HTTPException so the client gets a stable `code` and an actionable `hint`.
    """

    def __init__(self, status: int, code: str, message: str, hint: str | None = None,
                 headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        self.headers = headers or {}


def error_body(code: str, message: str, hint: str | None = None,
               fields: list | None = None) -> dict:
    err = {
        "code": code,
        "message": message,
        "hint": hint,
        "request_id": request_id_var.get(),
    }
    if fields:
        err["fields"] = fields
    return {"detail": message, "error": err}


def error_response(status: int, code: str, message: str, hint: str | None = None,
                   fields: list | None = None, headers: dict | None = None) -> JSONResponse:
    h = dict(headers or {})
    h["X-Request-ID"] = request_id_var.get()
    return JSONResponse(status_code=status, content=error_body(code, message, hint, fields), headers=h)


# --------------------------------------------------------------------------
# Turning arbitrary exceptions from a screening source into something a
# compliance analyst can act on, WITHOUT leaking file paths or stack frames.
# The full traceback always goes to the server log under the request ID.
# --------------------------------------------------------------------------

def describe_exception(exc: BaseException) -> tuple[str, str, str]:
    """Returns (code, human message, hint) for an exception raised inside a source check."""
    name = type(exc).__name__
    module = type(exc).__module__ or ""

    if isinstance(exc, ET.ParseError):
        return ("SOURCE_DATA_CORRUPT",
                "The cached list file could not be parsed (it may be truncated or corrupt).",
                "Re-run the refresh for this list from the Lists & audit tab.")
    if isinstance(exc, FileNotFoundError):
        return ("SOURCE_FILE_MISSING",
                "A file this check needs is missing on the server.",
                "Refresh the list, or re-upload it. If this keeps happening, check the persistent disk is mounted.")
    if module.startswith("requests") or module.startswith("urllib3"):
        if "Timeout" in name:
            return ("UPSTREAM_TIMEOUT", "The external service took too long to respond.",
                    "Try again in a minute. If it persists the provider may be down.")
        if "HTTPError" in name:
            status = getattr(getattr(exc, "response", None), "status_code", "?")
            return (f"UPSTREAM_HTTP_{status}", f"The external service answered with HTTP {status}.",
                    "Try again later; if it persists, the endpoint or credentials may have changed.")
        return ("UPSTREAM_UNREACHABLE", "Could not reach the external service.",
                "The server may have no outbound network access, or the provider is down.")
    if module.startswith("sqlite3"):
        return ("DATABASE_ERROR", "A database operation failed.",
                "Check the persistent disk has free space and the database file is writable.")
    if isinstance(exc, MemoryError):
        return ("OUT_OF_MEMORY", "The server ran out of memory processing this list.",
                "Consider a larger Render plan.")
    return (f"UNEXPECTED_{name.upper()}", f"Unexpected {name} while running this check.",
            "Send the reference ID to whoever maintains this tool; the full traceback is in the server log.")


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def _clean_validation_errors(exc: RequestValidationError) -> list[dict]:
    fields = []
    for e in exc.errors():
        loc = [str(p) for p in e.get("loc", ()) if p not in ("body", "query", "path")]
        msg = e.get("msg", "Invalid value")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        # NOTE: deliberately NOT including e["input"] — that is the raw
        # submitted value (an applicant's name/CNIC) and must not be echoed.
        fields.append({"field": ".".join(loc) or "request", "message": msg})
    return fields


def install(app: FastAPI) -> None:
    """Registers exception handlers. The request-context middleware is in `request_context`."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        if exc.status >= 500:
            logger.error("AppError %s: %s", exc.code, exc.message)
        return error_response(exc.status, exc.code, exc.message, exc.hint, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 401: "UNAUTHORIZED",
                403: "FORBIDDEN", 413: "PAYLOAD_TOO_LARGE"}.get(exc.status_code, f"HTTP_{exc.status_code}")
        hint = None
        if exc.status_code == 404 and detail == "Not Found":
            detail = "That endpoint does not exist."
            hint = "The frontend and backend may be running different versions — redeploy both."
        return error_response(exc.status_code, code, detail, hint, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = _clean_validation_errors(exc)
        summary = "; ".join(f"{f['field']}: {f['message']}" for f in fields[:3]) or "Invalid request"
        return error_response(422, "VALIDATION_ERROR", summary,
                              "Fix the highlighted field(s) and try again.", fields=fields)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(request: Request, exc: RateLimitExceeded):
        limit = str(getattr(exc, "detail", "") or "").strip()
        return error_response(
            429, "RATE_LIMITED",
            f"Too many requests ({limit})." if limit else "Too many requests.",
            "Wait a minute and try again. Limits exist to protect the public list publishers and applicant data.",
            headers={"Retry-After": "60"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Backstop only — request_context() normally catches first so that the
        # response passes back through the CORS middleware.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _internal_error_response()


def _internal_error_response() -> JSONResponse:
    return error_response(
        500, "INTERNAL_ERROR",
        "Something went wrong on the server.",
        "Quote the reference ID below to whoever maintains this tool — the full details are in the server log under that ID.",
    )


async def request_context(request: Request, call_next):
    """
    Assigns/propagates the request ID, logs one access line per request
    (method, path, status, duration — never query strings or bodies), and
    converts any uncaught exception into the structured 500 body.

    Must be registered as the INNERMOST middleware so the response it
    returns still travels back out through CORS/security-headers. Otherwise
    a crash produces a response with no CORS headers and the browser reports
    an opaque "Failed to fetch" instead of the real error.
    """
    incoming = request.headers.get("X-Request-ID", "")
    rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    request.state.request_id = rid
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            response = _internal_error_response()
        response.headers["X-Request-ID"] = rid
        ms = (time.perf_counter() - started) * 1000
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        if request.url.path != "/api/health":
            logger.log(level, "%s %s -> %s (%.0f ms)", request.method, request.url.path,
                       response.status_code, ms)
        return response
    finally:
        request_id_var.reset(token)
