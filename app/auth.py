"""
Sign in and access control.

Every request to the API (except /api/health) must carry TWO things:

  X-API-Key: <APP_API_KEY>            which app is calling. The frontend sends it automatically.
  Authorization: Bearer <access token>   who is using it. The person signs in with Supabase Auth
                                       (email and password) and the frontend sends the token it gets.

The app key is checked first, so a caller that is not the app learns nothing else. The token is then
verified and the person looked up in the `profiles` table:

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

The app key is NOT a secret (it is built into the frontend), so it never grants access by itself: a
request with the app key and no valid sign in is refused.

API_KEY is a different, secret key for a machine: the scheduled GitHub workflow that uploads the NACTA
list sends it as X-API-Key with no sign in. It is accepted on that one route and nowhere else.

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

API_KEY = os.environ.get("API_KEY")     # the machine credential for the scheduled NACTA upload

PROFILE_TTL_SECONDS = 10.0
_PROFILE_CACHE_MAX = 2000


@dataclass(frozen=True)
class AuthUser:
    id: str | None          # None for the API key
    email: str
    role: str               # "user" or "admin"
    status: str             # "pending", "approved" or "rejected"
    via: str = "token"      # "token" (a signed in person) or "service" (the API key on the NACTA upload)

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


def _matches(provided: str, expected: str) -> bool:
    # compared as bytes: compare_digest raises TypeError on non-ASCII str, which would turn a
    # mistyped or pasted key into a 500 instead of a clean 401
    return secrets.compare_digest(provided.encode("utf-8", "replace"), expected.encode("utf-8"))


def check_app_key(x_api_key: str | None) -> None:
    """The frontend must identify itself with APP_API_KEY. Refuses with a 401 that says which side to fix."""
    if not config.REQUIRE_APP_KEY:
        return
    if not config.APP_API_KEY:
        raise AppError(503, "AUTH_NOT_CONFIGURED", "The server is not set up to accept requests from the app.",
                       "Set APP_API_KEY on the backend (and the same value as VITE_API_KEY on the frontend).")
    if not x_api_key:
        raise AppError(401, "AUTH_MISSING_KEY", "This request did not include the app's access key.",
                       "The frontend sends it as X-API-Key. Set VITE_API_KEY on the frontend and redeploy it.")
    if not _matches(x_api_key, config.APP_API_KEY):
        raise AppError(401, "AUTH_INVALID_KEY", "The app's access key was rejected.",
                       "Check VITE_API_KEY on the frontend matches APP_API_KEY on the backend exactly (no trailing spaces).")


def _mark(request: Request, user: AuthUser) -> AuthUser:
    # the rate limiter counts per person, not per server address (behind a proxy everyone shares one)
    request.state.rate_key = f"user:{user.id or user.via}"
    return user


def authenticate(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AuthUser:
    """
    Whoever is signed in, whatever their status, provided the request comes from the app. Used by
    /api/me so a pending user can see where they stand.
    """
    check_app_key(x_api_key)
    token = _bearer(authorization)
    if not token:
        raise AppError(401, "AUTH_REQUIRED", "Sign in to continue.",
                       "Send the access token as 'Authorization: Bearer <token>'.")
    claims = verify_token(token)
    prof = _profile(claims["sub"], claims.get("email", ""))
    return _mark(request, AuthUser(claims["sub"], prof["email"] or claims.get("email", ""),
                                   prof["role"], prof["status"]))


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
    """
    The NACTA upload: the scheduled workflow with the secret API_KEY (no sign in), or a signed in
    admin using the app. The app key alone never gets in here: it is not the secret.
    """
    if not authorization and x_api_key and API_KEY and _matches(x_api_key, API_KEY):
        return _mark(request, AuthUser(None, "service", "admin", "approved", via="service"))
    return require_admin(require_approved(authenticate(request, authorization, x_api_key)))
