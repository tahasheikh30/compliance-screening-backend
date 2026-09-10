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
from fastapi import Header, HTTPException

API_KEY = os.environ.get("API_KEY")


def require_api_key(x_api_key: str = Header(default=None, alias="X-API-Key")):
    if not API_KEY:
        raise HTTPException(
            503,
            "Server is not configured with an API_KEY — refusing to serve "
            "requests until it is set. See README.",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "Missing or invalid API key")
    return True
