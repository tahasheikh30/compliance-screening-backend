"""
Talking to Supabase Auth as an administrator: removing a person's sign in account.

This needs SUPABASE_SERVICE_ROLE_KEY (a secret that stays on the backend). Deleting only our own profile row
would leave the person able to sign in again, so Delete removes the sign in account first.
"""

import requests

from app import config

_TIMEOUT_SECONDS = 10


class NotConfigured(Exception):
    """SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set."""


class AuthServiceError(Exception):
    """The authentication service refused or could not be reached."""


def configured() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SERVICE_ROLE_KEY)


def delete_auth_user(user_id: str) -> bool:
    """
    Remove the person's sign in account. True when it was removed, False when it was already gone (404: fine,
    that is the state we want). Raises NotConfigured or AuthServiceError.
    """
    if not configured():
        raise NotConfigured()
    key = config.SUPABASE_SERVICE_ROLE_KEY
    headers = {"apikey": key}
    if key.startswith("eyJ"):                 # a legacy service_role key is a JWT; the newer secret keys are not
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = requests.delete(f"{config.SUPABASE_URL}/auth/v1/admin/users/{user_id}", headers=headers,
                            timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise AuthServiceError(f"could not reach the authentication service ({type(exc).__name__})") from None
    if r.status_code in (200, 204):
        return True
    if r.status_code == 404:
        return False
    raise AuthServiceError(f"the authentication service answered {r.status_code}")
