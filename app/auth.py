"""
Sign in and access control.

People sign up and sign in with Supabase Auth (email and password). The frontend sends the access
token it gets as

    Authorization: Bearer <access token>

and this module checks it, then looks the person up in the `profiles` table:

  * status "pending"  : signed up, waiting for an admin. Can only call GET /api/me.
  * status "approved" : can screen applicants and see their own screening history.
  * status "rejected" : refused.
  * role "admin"      : an approved user who also sees everyone's history, approves new users,
                        and manages the lists.

Tokens are checked here, not by calling Supabase on every request: the signature is verified
with the project's public signing keys (fetched once from SUPABASE_URL and cached), or with
SUPABASE_JWT_SECRET for projects that still use the legacy shared secret. Expiry, audience and
issuer are checked too. A user's profile is cached for a few seconds, so an approval or a
rejection takes effect almost at once.

API_KEY (an X-API-Key header) is a machine credential, accepted only to upload the NACTA list
(the scheduled GitHub workflow uses it). It is NOT a way to read applicant data. Unless
ALLOW_API_KEY_FULL_ACCESS=true, which exists only to keep an older frontend working while it is
replaced.

Anything not configured fails closed: a request is refused, never let through.
"""

import os
import secrets
import threading
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, Request
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from app import config
from app import database as db
from app.errors import AppError, logger

API_KEY = os.environ.get("API_KEY")

PROFILE_TTL_SECONDS = 10.0
_PROFILE_CACHE_MAX = 2000


@dataclass(frozen=True)
class AuthUser:
    id: str | None          # None for the API key
    email: str
    role: str               # "user" or "admin"
    status: str             # "pending", "approved" or "rejected"
    via: str = "token"      # "token", "service" (API key on the NACTA upload) or "legacy" (API key, full access)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" and self.status == "approved"


# --------------------------------------------------------------------------
# Access tokens
# --------------------------------------------------------------------------

_jwks: PyJWKClient | None = None
_jwks_lock = threading.Lock()
_bad_kids: dict = {}          # key id -> time it was found unknown, so a flood of made up ids cannot make us refetch the key set over and over
_BAD_KID_SECONDS = 60.0


def _signing_key(token: str):
    """The public key that signed `token`, from the project's JWKS (cached by the client)."""
    global _jwks
    kid = jwt.get_unverified_header(token).get("kid") or ""
    seen = _bad_kids.get(kid)
    if seen and time.monotonic() - seen < _BAD_KID_SECONDS:
        raise PyJWKClientError("unknown signing key")
    with _jwks_lock:
        if _jwks is None:
            _jwks = PyJWKClient(f"{config.SUPABASE_URL}/auth/v1/.well-known/jwks.json",
                                cache_keys=True, lifespan=600, timeout=10)
        client = _jwks
    try:
        return client.get_signing_key_from_jwt(token).key
    except PyJWKClientConnectionError:
        raise
    except PyJWKClientError:
        if len(_bad_kids) > 500:
            _bad_kids.clear()
        _bad_kids[kid] = time.monotonic()
        raise


def verify_token(token: str) -> dict:
    """The verified claims of a Supabase access token, or an AppError explaining why not."""
    bad = AppError(401, "AUTH_INVALID_TOKEN", "Your sign in could not be verified.", "Sign in again.")
    try:
        alg = jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError:
        raise bad from None

    if alg == "HS256":
        if not config.SUPABASE_JWT_SECRET:
            raise AppError(503, "AUTH_NOT_CONFIGURED", "The server cannot check sign ins yet.",
                           "Set SUPABASE_JWT_SECRET on the backend (the project's legacy JWT secret).")
        key = config.SUPABASE_JWT_SECRET
    elif alg in ("ES256", "RS256", "EdDSA"):
        if not config.SUPABASE_URL:
            raise AppError(503, "AUTH_NOT_CONFIGURED", "The server cannot check sign ins yet.",
                           "Set SUPABASE_URL on the backend (https://<project>.supabase.co).")
        try:
            key = _signing_key(token)
        except PyJWKClientConnectionError:
            logger.error("Could not fetch the Supabase signing keys")
            raise AppError(503, "AUTH_UNAVAILABLE", "Sign in could not be checked right now.",
                           "Try again in a moment.") from None
        except (PyJWKClientError, jwt.PyJWTError):
            raise bad from None
    else:
        raise bad   # includes alg "none"

    try:
        claims = jwt.decode(
            token, key, algorithms=[alg], audience="authenticated", leeway=10,
            issuer=f"{config.SUPABASE_URL}/auth/v1" if config.SUPABASE_URL else None,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise AppError(401, "AUTH_TOKEN_EXPIRED", "Your session has expired.", "Sign in again.") from None
    except jwt.PyJWTError:
        raise bad from None

    if claims.get("role") != "authenticated" or claims.get("is_anonymous") or not claims.get("email"):
        raise bad
    return claims


# --------------------------------------------------------------------------
# Profiles (who is approved)
# --------------------------------------------------------------------------

_profiles: dict = {}
_profiles_lock = threading.Lock()


def invalidate_profile(user_id: str | None = None) -> None:
    with _profiles_lock:
        if user_id is None:
            _profiles.clear()
        else:
            _profiles.pop(str(user_id), None)


def _profile(user_id: str, email: str) -> dict:
    now = time.monotonic()
    with _profiles_lock:
        hit = _profiles.get(user_id)
    if hit and hit[0] > now:
        return hit[1]
    prof = db.get_profile(user_id)
    if prof is None or (email and prof["email"] != email):
        # a user the sign up trigger has not seen (pending), or an email that changed
        prof = db.upsert_profile(user_id, email)
    with _profiles_lock:
        if len(_profiles) >= _PROFILE_CACHE_MAX:
            _profiles.clear()
        _profiles[user_id] = (now + PROFILE_TTL_SECONDS, prof)
    return prof


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------

def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AppError(401, "AUTH_INVALID_TOKEN", "The Authorization header is not a Bearer token.", "Sign in again.")
    return token.strip()


def _key_matches(x_api_key: str) -> bool:
    # compared as bytes: compare_digest raises TypeError on non-ASCII str, which would turn a
    # mistyped or pasted key into a 500 instead of a clean 401
    return secrets.compare_digest(x_api_key.encode("utf-8", "replace"), API_KEY.encode("utf-8"))


def _check_key(x_api_key: str) -> None:
    if not API_KEY:
        raise AppError(503, "AUTH_NOT_CONFIGURED", "The server is not configured to accept an access key.",
                       "Set the API_KEY environment variable on the backend, or sign in with your account.")
    if not _key_matches(x_api_key):
        raise AppError(401, "AUTH_INVALID_KEY", "That access key was rejected.",
                       "Check it matches the backend's API_KEY exactly (no trailing spaces).")


def _mark(request: Request, user: AuthUser) -> AuthUser:
    # the rate limiter counts per person, not per server address (behind a proxy everyone shares one)
    request.state.rate_key = f"user:{user.id or user.via}"
    return user


def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthUser:
    """Whoever is signed in, whatever their status. Used by /api/me so a pending user can see where they stand."""
    token = _bearer(authorization)
    if token:
        claims = verify_token(token)
        prof = _profile(claims["sub"], claims.get("email", ""))
        return _mark(request, AuthUser(claims["sub"], prof["email"] or claims.get("email", ""),
                                       prof["role"], prof["status"]))
    if x_api_key:
        _check_key(x_api_key)
        if config.ALLOW_API_KEY_FULL_ACCESS:
            return _mark(request, AuthUser(None, "api-key", "admin", "approved", via="legacy"))
        raise AppError(403, "API_KEY_NOT_ACCEPTED", "An access key cannot be used for this request.",
                       "Sign in with your account.")
    raise AppError(401, "AUTH_REQUIRED", "Sign in to continue.", "Send the access token as 'Authorization: Bearer <token>'.")


def require_approved(user: AuthUser = Depends(authenticate)) -> AuthUser:
    if user.status == "pending":
        raise AppError(403, "ACCOUNT_PENDING", "Your account is waiting for approval by an administrator.",
                       "You can use the tool as soon as it has been approved.")
    if user.status == "rejected":
        raise AppError(403, "ACCOUNT_REJECTED", "Your account request was declined.",
                       "Contact an administrator if you think this is a mistake.")
    return user


def require_admin(user: AuthUser = Depends(require_approved)) -> AuthUser:
    if not user.is_admin:
        raise AppError(403, "ADMIN_ONLY", "Only an administrator can do this.")
    return user


def require_admin_or_service_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthUser:
    """The NACTA upload: an admin signed in, or the scheduled workflow with the API key."""
    if not authorization and x_api_key:
        _check_key(x_api_key)
        return _mark(request, AuthUser(None, "service", "admin", "approved", via="service"))
    user = authenticate(request, authorization, x_api_key)
    return require_admin(require_approved(user))
