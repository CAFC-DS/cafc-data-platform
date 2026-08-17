"""
Entry point: authenticate against DVMS, pull every fixture for a competition
+ season (including playoffs — see _collect_all_fixtures), and land them in
CAFC_DB.DVMS_RAW.FIXTURES + DVMS_RAW.ASSETS.

Asset *content* is downloaded inline for the confirmed-small feed types
(Opta F7/F24 event data, Second Spectrum metadata + physical summary/splits —
config.DOWNLOADABLE_ASSET_SUBTYPES). The raw frame-by-frame positional
tracking feeds (Second Spectrum Data JSON/XML, subtypes 38/39) are NOT
downloaded here — confirmed live at ~420MB/match, ~200GB/season, needs an
explicit storage decision (external/internal stage) before landing at all.
Video/panoramic/GeniusSports assets are never attempted (gigabytes). All of
these still get a metadata-only row (type/subtype/key/ready) in ASSETS.

The DVMS API's own "ready" flag is NOT used to gate downloads — confirmed
live that it reads false even for assets that download successfully.
Availability is determined empirically: attempt the download, treat a
failure as unavailable.

Rate limiting: a 2026-07-17 test run confirmed DVMS enforces anti-abuse
protection that can block even the login endpoint under sustained load —
see RateLimitedError. This script paces downloads (config.DOWNLOAD_DELAY_SECONDS)
and aborts immediately on any 403 rather than retrying — real DVMS rate
limits are unconfirmed, so pacing is a conservative default, not a tuned
value.

Repeatable / schedulable: safe to run this on a recurring schedule.
DVMS_RAW.FIXTURES/ASSETS are append-only (the platform convention — raw is
a replay buffer), but this script skips work that's already done rather
than reappending it every run: a fixture is only re-written if it's new or
its score/date changed since the last run, and an asset is only
(re-)attempted if it doesn't already have downloaded content (and is a
type we ever download in the first place — video/positional assets, never
downloaded by design, are written once and never revisited). See
_load_known_state(). This means a steady-state scheduled run mostly just
picks up newly-played matches and any previously-unready assets that have
since become available — not a ~550-fixture, ~1-hour re-download every
time.

Usage:
    python -m python.extract.dvms.load_dvms_fixtures [--triggered-by NAME]
        [--competition-id ID] [--season YYYY] [--skip-asset-download]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from snowflake.connector.pandas_tools import write_pandas

from python import _snowflake
from python.extract.dvms import config, dvms_auth, dvms_client

log = logging.getLogger("cafc.extract.dvms")

FIXTURES_TABLE = "FIXTURES"
ASSETS_TABLE = "ASSETS"

# Fixtures processed (and written) per batch, bounding memory for the
# downloaded asset content and giving incremental progress/resilience across
# a run that can take tens of minutes over ~550 fixtures.
BATCH_SIZE = 25


def _parse_dvms_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        log.warning("Could not parse fixture date %r", value)
        return None


def _open_run(cur, triggered_by: str) -> int:
    cur.execute(
        """
        INSERT INTO CAFC_DB.CORE.INGESTION_RUNS (SOURCE_SYSTEM, TRIGGERED_BY, NOTES)
        VALUES (%(src)s, %(by)s, %(notes)s)
        """,
        {"src": "DVMS", "by": triggered_by, "notes": "dvms fixtures extractor run"},
    )
    cur.execute("SELECT MAX(RUN_ID) FROM CAFC_DB.CORE.INGESTION_RUNS")
    return int(cur.fetchone()[0])


def _close_run(cur, run_id: int, status: str, notes: str = "") -> None:
    cur.execute(
        """
        UPDATE CAFC_DB.CORE.INGESTION_RUNS
           SET STATUS = %(status)s, FINISHED_AT = CURRENT_TIMESTAMP(), NOTES = %(notes)s
         WHERE RUN_ID = %(rid)s
        """,
        {"status": status, "notes": notes, "rid": run_id},
    )


class RateLimitedError(RuntimeError):
    """Raised when DVMS returns 403 on a request other than the initial
    authenticate() call. Confirmed live on 2026-07-17: this is DVMS's
    anti-abuse/rate-limiting protection, not a per-asset failure — retrying
    or (worse) re-authenticating more aggressively in response is what
    escalated a handful of 403s into the login endpoint itself getting
    blocked during testing. The only correct response is to stop the run
    entirely and let a human decide when/how to resume with more
    conservative pacing — never retry or re-auth automatically here.
    """


def _download_or_raise(session, competition_id: str, fixture_id: str, asset_id: str, subtype):
    """Attempt a download. Returns (None, None) for ordinary failures (e.g.
    a genuinely missing/expired asset), but raises RateLimitedError on a
    403 — see that class's docstring for why this must not be retried."""
    try:
        return dvms_client.download_asset(session, competition_id, fixture_id, asset_id)
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            raise RateLimitedError(
                f"403 downloading asset {asset_id} (fixture {fixture_id}, subType {subtype}) — "
                "treating as DVMS rate limiting, aborting rather than retrying."
            ) from exc
        log.warning("Download failed for asset %s (fixture %s, subType %s): %s", asset_id, fixture_id, subtype, exc)
        return None, None
    except Exception as exc:
        log.warning("Download failed for asset %s (fixture %s, subType %s): %s", asset_id, fixture_id, subtype, exc)
        return None, None


def _collect_all_fixtures(session, competition_id: str, season: str) -> list[dict]:
    """iter_fixtures() (by-competition pagination) caps at ~round 39 for
    reasons that don't surface as an error — confirmed live it misses the
    final ~90 fixtures including all playoff rounds. get_team_fixtures()
    does return a team's complete season including playoffs, so: seed a
    team-id list from the (incomplete) competition pull, then pull every
    team's complete fixture list and dedupe by fixtureId. A team appears in
    ~2x its fixture count of calls (once as the pulling team, implicitly
    included in opponents' pulls too), but dedup makes that free.
    """
    seed = list(dvms_client.iter_fixtures(session, competition_id, season))
    team_ids = set()
    for f in seed:
        for key in ("optaHomeTeamId", "optaAwayTeamId"):
            tid = f.get(key)
            if tid:
                team_ids.add(tid)
    log.info("Seed pull: %d fixtures, %d distinct teams", len(seed), len(team_ids))

    by_fixture_id = {f["fixtureId"]: f for f in seed if f.get("fixtureId")}
    for team_id in sorted(team_ids):
        time.sleep(config.DOWNLOAD_DELAY_SECONDS)
        team_fixtures = dvms_client.get_team_fixtures(session, competition_id, season, team_id)
        for f in team_fixtures:
            fid = f.get("fixtureId")
            if fid:
                by_fixture_id[fid] = f
        log.info("Team %s -> %d fixtures (running unique total: %d)", team_id, len(team_fixtures), len(by_fixture_id))

    return list(by_fixture_id.values())


def _load_known_state(conn, schema: str) -> tuple[dict, set]:
    """One-time query at the start of a run: what does Snowflake already
    have? Returns (fixture_state, done_asset_ids):
      - fixture_state: {fixture_id: (home_score, away_score, match_date)}
        from each fixture's most recent row — used to skip re-writing a
        fixture whose result/date hasn't changed.
      - done_asset_ids: asset_ids that need no further work this run,
        either because they already have downloaded content, or because
        their subtype is never downloaded (video/positional) and they've
        been recorded at least once already.
    This is what makes repeat/scheduled runs cheap: without it, every run
    would re-fetch and re-attempt everything from scratch.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT FIXTURE_ID, HOME_SCORE, AWAY_SCORE, MATCH_DATE
            FROM CAFC_DB.{schema}.FIXTURES
            QUALIFY ROW_NUMBER() OVER (PARTITION BY FIXTURE_ID ORDER BY LOADED_AT DESC) = 1
            """
        )
        fixture_state = {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}

        cur.execute(
            f"""
            SELECT ASSET_ID, MAX(ASSET_SUBTYPE), MAX(IFF(RAW_PAYLOAD IS NOT NULL, 1, 0))
            FROM CAFC_DB.{schema}.ASSETS
            GROUP BY ASSET_ID
            """
        )
        done_asset_ids = {
            asset_id
            for asset_id, subtype, has_content in cur.fetchall()
            if has_content or subtype not in config.DOWNLOADABLE_ASSET_SUBTYPES
        }
    log.info("Known state: %d fixtures previously seen, %d assets need no further work.", len(fixture_state), len(done_asset_ids))
    return fixture_state, done_asset_ids


def _fixture_row(fixture: dict, competition_id: str, season: str, run_id: int, loaded_at) -> dict:
    return {
        "FIXTURE_ID": fixture.get("fixtureId"),
        # From the request params, not fixture["competition"]/["optaSeason"]:
        # those body fields aren't guaranteed to match what was actually
        # queried (undocumented semantics), and this table should reflect
        # what was fetched.
        "COMPETITION_ID": competition_id,
        "SEASON": season,
        "OPTA_MATCH_ID": fixture.get("optaMatchId"),
        "OPTA_HOME_TEAM_ID": fixture.get("optaHomeTeamId"),
        "OPTA_AWAY_TEAM_ID": fixture.get("optaAwayTeamId"),
        "HOME_TEAM_NAME": fixture.get("homeTeamName"),
        "AWAY_TEAM_NAME": fixture.get("awayTeamName"),
        "MATCH_DATE": _parse_dvms_date(fixture.get("date")),
        "ROUND": fixture.get("round"),
        "HOME_SCORE": fixture.get("homeScore"),
        "AWAY_SCORE": fixture.get("awayScore"),
        "VENUE_ID": fixture.get("venueId"),
        "VENUE_NAME": fixture.get("venueName"),
        "NUM_ASSETS_AVAILABLE": fixture.get("numAssetsAvailable"),
        "RAW_PAYLOAD": json.dumps(fixture),
        "LOADED_AT": loaded_at,
        "INGESTION_RUN_ID": run_id,
    }


def _asset_rows(session, competition_id: str, fixture: dict, run_id: int, loaded_at, download: bool, done_asset_ids: set) -> list[dict]:
    fixture_id = fixture.get("fixtureId")
    rows = []
    for asset in fixture.get("assets") or []:
        asset_id = asset.get("assetId")
        if asset_id in done_asset_ids:
            continue  # already have content, or never-downloaded type already recorded

        info = asset.get("info") or {}
        subtype = asset.get("subType")
        source_url = None
        raw_payload = None

        if download and asset_id and subtype in config.DOWNLOADABLE_ASSET_SUBTYPES:
            # Deliberate pacing, not a rate-limit workaround we're guessing
            # at: DVMS's real limits are unknown (see RateLimitedError), so
            # this stays conservative until confirmed otherwise.
            time.sleep(config.DOWNLOAD_DELAY_SECONDS)
            source_url, raw_payload = _download_or_raise(session, competition_id, fixture_id, asset_id, subtype)

        rows.append(
            {
                "FIXTURE_ID": fixture_id,
                "ASSET_ID": asset_id,
                "ASSET_TYPE": asset.get("type"),
                "ASSET_SUBTYPE": subtype,
                "ASSET_KEY": asset.get("key"),
                "READY": asset.get("ready"),
                "SIZE_BYTES": info.get("sizeBytes"),
                "FILENAME": info.get("filename"),
                "SOURCE_URL": source_url,
                "RAW_PAYLOAD": raw_payload,
                "LOADED_AT": loaded_at,
                "INGESTION_RUN_ID": run_id,
            }
        )
    return rows


def _last_n_team_fixtures(session, competition_id: str, season: str, opta_team_ids: str, n: int) -> list[dict]:
    """Most recent N played fixtures for one or more comma-separated teams.

    DVMS's competition-wide endpoint can temporarily return an empty result for
    a newly started season. Its per-team endpoint remains available, so a
    comma-separated list lets a single authenticated run recover a whole round
    without repeatedly authenticating or duplicating shared fixtures.
    """
    fixtures_by_id: dict[str, dict] = {}
    for opta_team_id in (value.strip() for value in opta_team_ids.split(",")):
        if not opta_team_id:
            continue
        fixtures = dvms_client.get_team_fixtures(session, competition_id, season, opta_team_id)
        played = [f for f in fixtures if f.get("homeScore") not in (None, "") and f.get("awayScore") not in (None, "")]
        played.sort(key=lambda f: f.get("date") or "", reverse=True)
        for fixture in played[:n]:
            fixture_id = fixture.get("fixtureId")
            if fixture_id:
                fixtures_by_id[fixture_id] = fixture
        log.info("Team %s -> %d most-recent played fixture(s).", opta_team_id, min(len(played), n))
        time.sleep(config.DOWNLOAD_DELAY_SECONDS)
    return list(fixtures_by_id.values())


def run(
    triggered_by: str,
    competition_id: str,
    season: str,
    download_assets: bool = True,
    team_id: str | None = None,
    last_n: int | None = None,
) -> int:
    # One session for the whole run. Do NOT re-authenticate mid-run: testing
    # on 2026-07-17 showed extra authenticate() calls under load are what
    # escalated ordinary 403s into the login endpoint itself getting
    # blocked (see RateLimitedError). If this run hits a 403, it aborts —
    # the fix is slower pacing on the *next* run, not retrying this one.
    token = dvms_auth.authenticate()
    session = dvms_client.build_session(token)

    conn = _snowflake.get_connection(schema=config.SNOWFLAKE_SCHEMA)
    run_id = None
    try:
        with conn.cursor() as cur:
            run_id = _open_run(cur, triggered_by)
        conn.commit()
        log.info("Opened INGESTION_RUNS.RUN_ID = %d", run_id)

        fixture_state, done_asset_ids = _load_known_state(conn, config.SNOWFLAKE_SCHEMA)

        if team_id and last_n:
            all_fixtures = _last_n_team_fixtures(session, competition_id, season, team_id, last_n)
            log.info("Scoped to team=%s, last %d played fixture(s): %d found.", team_id, last_n, len(all_fixtures))
        else:
            all_fixtures = _collect_all_fixtures(session, competition_id, season)
            log.info("Total unique fixtures for competition=%s season=%s: %d", competition_id, season, len(all_fixtures))

        if not all_fixtures:
            status, notes = "FAILED", "0 fixtures returned — check competition_id/season/credentials."
            log.warning(notes)
            with conn.cursor() as cur:
                _close_run(cur, run_id, status, notes)
            conn.commit()
            return run_id

        total_fixtures, total_assets, downloaded_bytes = 0, 0, 0
        for start in range(0, len(all_fixtures), BATCH_SIZE):
            batch = all_fixtures[start : start + BATCH_SIZE]
            loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)  # naive — see write_pandas note below

            # Only (re-)write a fixture if it's new or its score/date changed
            # since the last run — see _load_known_state().
            fixture_rows = []
            for f in batch:
                current = (f.get("homeScore"), f.get("awayScore"), _parse_dvms_date(f.get("date")))
                if fixture_state.get(f.get("fixtureId")) != current:
                    fixture_rows.append(_fixture_row(f, competition_id, season, run_id, loaded_at))

            asset_rows = []
            for f in batch:
                asset_rows.extend(_asset_rows(session, competition_id, f, run_id, loaded_at, download_assets, done_asset_ids))

            # use_logical_type=True + consistently-naive datetimes: mixing a
            # tz-aware and a naive datetime column in one write_pandas() call
            # corrupted every MATCH_DATE on the first real run (silent, only
            # a warning — see git history). Keep both fixed.
            if fixture_rows:
                write_pandas(
                    conn=conn, df=pd.DataFrame(fixture_rows), table_name=FIXTURES_TABLE,
                    database="CAFC_DB", schema=config.SNOWFLAKE_SCHEMA,
                    auto_create_table=False, overwrite=False, use_logical_type=True,
                )
                total_fixtures += len(fixture_rows)

            if asset_rows:
                write_pandas(
                    conn=conn, df=pd.DataFrame(asset_rows), table_name=ASSETS_TABLE,
                    database="CAFC_DB", schema=config.SNOWFLAKE_SCHEMA,
                    auto_create_table=False, overwrite=False, use_logical_type=True,
                )
                total_assets += len(asset_rows)
                downloaded_bytes += sum(len(r["RAW_PAYLOAD"] or "") for r in asset_rows)

            log.info(
                "Batch %d-%d done. Running total: %d fixtures, %d assets, ~%.1fMB downloaded.",
                start, start + len(batch), total_fixtures, total_assets, downloaded_bytes / 1e6,
            )

        status = "SUCCESS"
        notes = f"Loaded {total_fixtures} fixtures, {total_assets} assets, ~{downloaded_bytes/1e6:.1f}MB asset content."
        log.info(notes)

        with conn.cursor() as cur:
            _close_run(cur, run_id, status, notes)
        conn.commit()
        return run_id
    except Exception as exc:
        if run_id is not None:
            with conn.cursor() as cur:
                _close_run(cur, run_id, "FAILED", notes=str(exc)[:1000])
            conn.commit()
        raise
    finally:
        conn.close()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract DVMS fixtures + assets into CAFC_DB.DVMS_RAW.")
    p.add_argument("--triggered-by", default=os.getenv("USER", "manual"),
                    help="Free-text label written to INGESTION_RUNS.TRIGGERED_BY.")
    p.add_argument("--competition-id", default=config.DVMS_COMPETITION_ID,
                    help="Hudl competition id (default: EFL Championship).")
    p.add_argument("--season", default=config.DVMS_SEASON,
                    help="DVMS season key, e.g. 2025 for the 2025-26 season.")
    p.add_argument("--skip-asset-download", action="store_true",
                    help="Land asset metadata only, skip downloading content (faster).")
    p.add_argument("--team-id", default=None,
                    help="One or more comma-separated Opta team ids, e.g. t33 or t33,t24. Combine with --last-n-games for a targeted pull instead of the full competition backfill.")
    p.add_argument("--last-n-games", type=int, default=None,
                    help="With --team-id: only that team's most recent N played fixtures.")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _build_argparser().parse_args()
    run(
        triggered_by=args.triggered_by,
        competition_id=args.competition_id,
        season=args.season,
        download_assets=not args.skip_asset_download,
        team_id=args.team_id,
        last_n=args.last_n_games,
    )


if __name__ == "__main__":
    main()
