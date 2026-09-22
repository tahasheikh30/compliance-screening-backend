"""
Minimal API-key authentication.

This is an internal compliance tool handling applicant PII (names, CNICs)
and generating evidence for adverse-action decisions — it must not be a
publicly open API. A shared API key is the simplest thing that's actually
secure for a small internal team; for anything beyond a pilot, replace this
with proper per-user auth (e.g. your org's SSO/Azure AD, since Packages
Group is presumably a Microsoft shop) so actions are attributable to a
specific compliance analyst, not just "someone with the key".

Set API_KEY in the environment. Requests must send it as:
    X-API-Key: <the key>

If API_KEY is not set, the app still starts (so local dev doesn't need
ceremony) but every protected endpoint returns 503 until it's configured —
fails closed, not open.
"""

import os
import secrets
from fastapi import Header
from app.errors import AppError

API_KEY = os.environ.get("API_KEY")


def require_api_key(x_api_key: str = Header(default=None, alias="X-API-Key")):
    if not API_KEY:
        raise AppError(
            503, "AUTH_NOT_CONFIGURED",
            "Server is not configured with an API_KEY — refusing to serve requests until it is set.",
            "Set the API_KEY environment variable on the backend (Render dashboard) and redeploy. See README.",
        )
    if not x_api_key:
        raise AppError(401, "AUTH_MISSING_KEY", "No access key was sent with this request.",
                       "Enter the access key again.")
    # Compare as bytes: compare_digest raises TypeError on non-ASCII str, which
    # would turn a mistyped/pasted key into a 500 instead of a clean 401.
    if not secrets.compare_digest(x_api_key.encode("utf-8", "replace"), API_KEY.encode("utf-8")):
        raise AppError(401, "AUTH_INVALID_KEY", "That access key was rejected.",
                       "Check it matches the backend's API_KEY exactly (no trailing spaces).")
    return True
