"""
Hudl DVMS API client: competitions, fixtures (paginated), and asset downloads.

Matches the documented API at https://dvms.premierleague.com/dvms/api-docs,
confirmed against live responses on 2026-07-17.
"""
from __future__ import annotations

import logging
from typing import Iterator

import requests

from python.extract.dvms import config

log = logging.getLogger("cafc.extract.dvms")


def build_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Hudl-AuthToken": token,
        }
    )
    return session


def get_competitions(session: requests.Session) -> list[dict]:
    response = session.get(f"{config.DVMS_BASE_URL}/dvms/competitions", timeout=config.REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def iter_fixtures(
    session: requests.Session,
    competition_id: str,
    season: str,
    limit: int = config.FIXTURES_PAGE_LIMIT,
) -> Iterator[dict]:
    """Paginate POST /dvms/{competitionId}/fixtures/{season} until a page
    returns no fixtures. Yields raw fixture dicts (each already carries its
    own nested "assets" list — no separate per-match listing call exists).

    Confirmed live on 2026-07-17: this by-competition endpoint stops after
    ~457 fixtures / round 39 for a 46-round-plus-playoffs season, for reasons
    that don't show up as an error (clean pagination end, not a truncation
    artifact) — cause unconfirmed. get_team_fixtures() below does return the
    complete season including playoffs, so that's the reliable path; this
    function is kept mainly to cheaply harvest the season's team-id list.
    """
    url = f"{config.DVMS_BASE_URL}/dvms/{competition_id}/fixtures/{season}"
    page = 1
    while True:
        response = session.post(
            url, json={"pageNumber": page, "limit": limit}, timeout=config.REQUEST_TIMEOUT
        )
        response.raise_for_status()
        fixtures = response.json().get("fixtures") or []
        log.info("Fixtures page %d -> %d fixtures", page, len(fixtures))
        if not fixtures:
            return
        yield from fixtures
        page += 1


def get_team_fixtures(
    session: requests.Session, competition_id: str, season: str, opta_team_id: str
) -> list[dict]:
    """POST /dvms/{competitionId}/fixtures/getTeamFixtures/{season}?optaTeamIds=...
    Confirmed live: returns a team's complete season including playoffs (49
    fixtures for a team that reached the Championship final, vs. the 39
    rounds iter_fixtures() caps at) — no pagination needed, a season's worth
    fits in one response."""
    response = session.post(
        f"{config.DVMS_BASE_URL}/dvms/{competition_id}/fixtures/getTeamFixtures/{season}",
        params={"optaTeamIds": opta_team_id},
        timeout=config.REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("fixtures") or []


def download_asset(
    session: requests.Session,
    competition_id: str,
    fixture_id: str,
    asset_id: str,
    max_bytes: int = config.MAX_INLINE_ASSET_BYTES,
):
    """GET /dvms/{competitionId}/fixtures/{fixtureId}/download/{assetId},
    streamed. Returns (final_url, body_text) on success, or (None, None) if
    the payload exceeds max_bytes.

    This is a real read-loop guard, not just a Content-Length check: the
    signed CloudFront redirect this endpoint lands on doesn't always send
    Content-Length, and the fixtures response's own info.sizeBytes is null
    for every non-video asset (confirmed live) — subType routing plus this
    streamed guard is the only reliable way to avoid pulling a ~420MB
    positional-tracking file into memory.
    """
    with session.get(
        f"{config.DVMS_BASE_URL}/dvms/{competition_id}/fixtures/{fixture_id}/download/{asset_id}",
        timeout=config.DOWNLOAD_TIMEOUT,
        stream=True,
    ) as response:
        response.raise_for_status()

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            log.warning("Asset %s: Content-Length=%s exceeds %d bytes, skipping", asset_id, content_length, max_bytes)
            return None, None

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                log.warning("Asset %s exceeded %d bytes mid-stream, skipping", asset_id, max_bytes)
                return None, None
            chunks.append(chunk)

        body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        return response.url, body
