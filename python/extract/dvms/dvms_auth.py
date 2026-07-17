"""
Authentication against the documented Hudl DVMS API.

POST /api/v2/authenticate with {"username", "password"} returns a token used
as the Hudl-AuthToken header on every subsequent call. Confirmed live against
the real API on 2026-07-17 — this is a proper machine auth endpoint (see
https://dvms.premierleague.com/dvms/api-docs), not a browser login form.
"""
from __future__ import annotations

import requests

from python.extract.dvms import config


def authenticate() -> str:
    """Return a live Hudl-AuthToken. Raises if credentials are missing or
    the API doesn't return a token."""
    if not config.DVMS_USERNAME or not config.DVMS_PASSWORD:
        raise RuntimeError(
            "DVMS_USERNAME / DVMS_PASSWORD not set — add them to "
            "python/extract/dvms/.env (gitignored)."
        )

    response = requests.post(
        f"{config.DVMS_BASE_URL}/api/v2/authenticate",
        json={"username": config.DVMS_USERNAME, "password": config.DVMS_PASSWORD},
        headers={"Content-Type": "application/json"},
        timeout=config.REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    token = response.json().get("token")
    if not token:
        raise RuntimeError(
            "DVMS authenticate call succeeded but returned no 'token' field — "
            "response shape may have changed from the documented API."
        )
    return token
