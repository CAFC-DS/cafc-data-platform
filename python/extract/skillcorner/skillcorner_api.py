"""
SkillCorner API client.

Auth was NOT documented anywhere accessible (the public docs page at
skillcorner.com/api/docs/ is behind an OAuth login wall) -- determined
empirically against the live API with the club's API key:
  - Header-based schemes (Authorization: Token/Bearer/Api-Key, X-Api-Key)
    all fail -- Bearer specifically returns a JWT-specific rejection
    ("Given token not valid for any token type"), confirming the API
    recognizes Bearer as a scheme but expects a JWT access token, not a
    static key, there.
  - HTTP Basic auth (key as username or password) fails ("Invalid
    username/password").
  - What works: a plain `token` query parameter on every request, e.g.
    GET /api/competitions/?token=<key>. Confirmed live returning real data
    (446 competitions, 3005+ matches visible to this account).

This means every request in this module appends {"token": SKILLCORNER_API_KEY}
to its params rather than setting a header.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Optional

import requests

import config


def _params(extra: Optional[dict] = None) -> dict:
    p = {"token": config.SKILLCORNER_API_KEY}
    if extra:
        p.update(extra)
    return p


def get_competitions(params: Optional[dict] = None) -> dict:
    r = requests.get(f"{config.SKILLCORNER_BASE_URL}/competitions/", params=_params(params),
                      timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_competition_editions(params: Optional[dict] = None) -> dict:
    """No server-side filter by competition id exists (confirmed: passing
    competition=<id> to this endpoint 400s) -- fetch and filter client-side."""
    r = requests.get(f"{config.SKILLCORNER_BASE_URL}/competition_editions/", params=_params(params),
                      timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_matches(competition_edition_id: int, limit: int = 500, offset: int = 0) -> dict:
    """The matches endpoint's own filter param is `competition_edition`
    (singular, no _id suffix) even though the response field is
    `competition_edition_id` -- confirmed live, the id-suffixed name 400s."""
    r = requests.get(
        f"{config.SKILLCORNER_BASE_URL}/matches/",
        params=_params({"competition_edition": competition_edition_id, "limit": limit, "offset": offset}),
        timeout=config.REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_available_competition_editions() -> list[dict[str, Any]]:
    """Discover which (competition, competition_edition) pairs this account
    actually has match data for, rather than trusting /competition_editions/'s
    full catalogue -- confirmed live that most listed editions (including
    everything before 2023/24) return zero matches for this account. Pages
    through every match the account can see (3,005 confirmed live) and
    reduces to the distinct editions they belong to; not cheap, but this is
    a discovery/planning call, not something to run per extraction.
    """
    seen: dict[int, dict[str, Any]] = {}
    offset = 0
    while True:
        resp = requests.get(
            f"{config.SKILLCORNER_BASE_URL}/matches/",
            params=_params({"limit": 500, "offset": offset}),
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break
        for m in results:
            eid = m["competition_edition_id"]
            seen.setdefault(eid, {
                "competition_id": m["competition_id"],
                "competition_edition_id": eid,
                "match_count": 0,
            })
            seen[eid]["match_count"] += 1
        offset += 500
        if offset >= data.get("count", 0):
            break
    return list(seen.values())


def get_match_physical(match_id: int) -> list[dict[str, Any]]:
    """GET /match/{id}/physical/ -- confirmed live returns CSV text (not
    JSON, unlike every other endpoint in this client), one row per player
    who featured in the match. 178 columns: identity/context columns plus
    physical metrics repeated across {overall, TIP, OTIP} possession splits
    x {full match, first half, second half} period splits."""
    r = requests.get(f"{config.SKILLCORNER_BASE_URL}/match/{match_id}/physical/", params=_params(),
                      timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return list(reader)
