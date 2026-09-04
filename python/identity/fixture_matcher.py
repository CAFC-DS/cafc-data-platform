"""
Fixture identity linker.

Links IMPECT_RAW.MATCHES rows that aren't yet in CORE.FIXTURE_IDENTITIES into
CORE.FIXTURES / CORE.FIXTURE_IDENTITIES.

Unlike player identity (python/identity/matcher.py), fixture identity is
deterministic and unambiguous: an IMPECT match id is exactly one real-world
fixture, and cafc_squad_id == impect_squad_id today (no squad matcher exists
yet — dbt/models/canonical/dimensions/core_squads.sql, "placeholder until
squad identity is resolved"). So every unlinked IMPECT match simply mints a
new CAFC_FIXTURE_ID; there is no LINK_EXISTING / AMBIGUOUS branch.

Women's competitions are excluded platform-wide. Matches aren't gendered
directly — gender lives on the squad — so a fixture is excluded unless both
the home and away squad are GENDER='MALE' in the deduped core_squads
dimension (CORE.CORE_SQUADS), mirroring matcher.py's --include-womens.

Skipped, not minted: rows with a NULL home/away squad id or a SCHEDULEDDATE
that doesn't parse — these are IMPECT data-quality gaps, not this script's
job to guess at. Logged and left unlinked; safe to re-run once the raw data
is fixed upstream.

Run with the default (no --apply) for a read-only count; --apply to write.
"""

from __future__ import annotations

import argparse
import logging

from python import _snowflake

log = logging.getLogger("cafc.identity.fixture_matcher")


def _gender_clause(include_womens: bool) -> str:
    return "" if include_womens else "AND hs.GENDER = 'MALE' AND aws.GENDER = 'MALE'"


def fetch_unlinked(cur, include_womens: bool) -> list[tuple]:
    """Every IMPECT match not yet in FIXTURE_IDENTITIES, with usable squad ids
    and a parseable date. Returns (match_id, home_squad_id, away_squad_id,
    scheduled_at) tuples."""
    cur.execute(f"""
        SELECT
            m.ID,
            m.HOMESQUADID,
            m.AWAYSQUADID,
            TRY_TO_TIMESTAMP_NTZ(m.SCHEDULEDDATE)
        FROM CAFC_DB.IMPECT_RAW.MATCHES m
        JOIN CAFC_DB.CORE.CORE_SQUADS hs  ON hs.CAFC_SQUAD_ID  = m.HOMESQUADID
        JOIN CAFC_DB.CORE.CORE_SQUADS aws ON aws.CAFC_SQUAD_ID = m.AWAYSQUADID
        WHERE m.HOMESQUADID IS NOT NULL
          AND m.AWAYSQUADID IS NOT NULL
          AND TRY_TO_TIMESTAMP_NTZ(m.SCHEDULEDDATE) IS NOT NULL
          {_gender_clause(include_womens)}
          AND NOT EXISTS (
              SELECT 1 FROM CAFC_DB.CORE.FIXTURE_IDENTITIES fi
              WHERE fi.SOURCE_SYSTEM = 'IMPECT'
                AND fi.SOURCE_FIXTURE_ID = TO_VARCHAR(m.ID)
          )
        ORDER BY m.ID
    """)
    return cur.fetchall()


def count_skipped(cur, include_womens: bool) -> dict[str, int]:
    """Diagnostics only — how many unlinked matches are being left out and why."""
    cur.execute(f"""
        SELECT
            COUNT_IF(m.HOMESQUADID IS NULL OR m.AWAYSQUADID IS NULL) AS missing_squad,
            COUNT_IF(TRY_TO_TIMESTAMP_NTZ(m.SCHEDULEDDATE) IS NULL) AS bad_date
        FROM CAFC_DB.IMPECT_RAW.MATCHES m
        WHERE NOT EXISTS (
            SELECT 1 FROM CAFC_DB.CORE.FIXTURE_IDENTITIES fi
            WHERE fi.SOURCE_SYSTEM = 'IMPECT'
              AND fi.SOURCE_FIXTURE_ID = TO_VARCHAR(m.ID)
        )
    """)
    row = cur.fetchone()
    return {"missing_squad_id": row[0], "unparseable_date": row[1]}


def mint_fixtures_bulk(cur, rows: list[tuple], run_id: int, batch_size: int = 5000) -> int:
    """Batch-insert CORE.FIXTURES + CORE.FIXTURE_IDENTITIES for `rows`
    (match_id, home_squad_id, away_squad_id, scheduled_at). Mints one
    CAFC_FIXTURE_ID per row via a single sequence range call, mirroring
    python/identity/mint.py's mint_players_bulk. Caller owns the transaction."""
    if not rows:
        return 0

    total = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        n = len(chunk)

        # One sequence call yields n distinct, monotonically increasing values.
        cur.execute(
            "SELECT CAFC_DB.CORE.CAFC_FIXTURE_ID_SEQ.NEXTVAL FROM TABLE(GENERATOR(ROWCOUNT => %(n)s))",
            {"n": n},
        )
        new_ids = sorted(r[0] for r in cur.fetchall())
        if len(new_ids) != n:
            raise RuntimeError(
                f"sequence yielded {len(new_ids)} values for a {n}-row batch"
            )

        fixture_rows = [
            {
                "cafc_fixture_id": cafc_id,
                "home_squad_id": home,
                "away_squad_id": away,
                "fixture_date": sched,
            }
            for cafc_id, (_match_id, home, away, sched) in zip(new_ids, chunk)
        ]
        cur.executemany(
            """
            INSERT INTO CAFC_DB.CORE.FIXTURES
              (CAFC_FIXTURE_ID, HOME_SQUAD_ID, AWAY_SQUAD_ID, FIXTURE_DATE, CREATED_FROM_SOURCE, IS_ACTIVE)
            VALUES (%(cafc_fixture_id)s, %(home_squad_id)s, %(away_squad_id)s, %(fixture_date)s, 'IMPECT', TRUE)
            """,
            fixture_rows,
        )

        identity_rows = [
            {
                "cafc_fixture_id": cafc_id,
                "source_fixture_id": str(match_id),
                "source_home_squad_id": str(home),
                "source_away_squad_id": str(away),
                "source_fixture_date": sched,
            }
            for cafc_id, (match_id, home, away, sched) in zip(new_ids, chunk)
        ]
        cur.executemany(
            """
            INSERT INTO CAFC_DB.CORE.FIXTURE_IDENTITIES
              (CAFC_FIXTURE_ID, SOURCE_SYSTEM, SOURCE_FIXTURE_ID, SOURCE_CONTEXT,
               SOURCE_HOME_SQUAD_ID, SOURCE_AWAY_SQUAD_ID, SOURCE_FIXTURE_DATE,
               MATCH_CONFIDENCE, IS_PRIMARY)
            VALUES (%(cafc_fixture_id)s, 'IMPECT', %(source_fixture_id)s, 'fixture_matcher.py',
                    %(source_home_squad_id)s, %(source_away_squad_id)s, %(source_fixture_date)s,
                    100, TRUE)
            """,
            identity_rows,
        )
        total += n
        log.info("  minted %d/%d", total, len(rows))

    return total


def run(dry_run: bool = True, include_womens: bool = False, run_id: int | None = None) -> dict[str, int]:
    conn = _snowflake.get_connection()
    try:
        with conn.cursor() as cur:
            log.info("Fetching unlinked IMPECT fixtures…")
            rows = fetch_unlinked(cur, include_womens)
            skipped = count_skipped(cur, include_womens)
            log.info("  %d linkable%s.", len(rows), "" if include_womens else " (women's excluded)")
            log.info("  skipped: %s", skipped)

            if dry_run:
                print()
                print("=" * 72)
                print("  DRY RUN — no writes performed")
                print("=" * 72)
                print(f"  Linkable IMPECT fixtures to mint: {len(rows)}")
                print(f"  Skipped (missing squad id):       {skipped['missing_squad_id']}")
                print(f"  Skipped (unparseable date):       {skipped['unparseable_date']}")
                print()
                return {"minted": 0, "skipped_missing_squad": skipped["missing_squad_id"],
                        "skipped_bad_date": skipped["unparseable_date"], "would_mint": len(rows)}

            owns_envelope = run_id is None
            if owns_envelope:
                cur.execute(
                    """
                    INSERT INTO CAFC_DB.CORE.INGESTION_RUNS (SOURCE_SYSTEM, TRIGGERED_BY, NOTES)
                    VALUES ('IMPECT', %(by)s, %(notes)s)
                    """,
                    {"by": "fixture_matcher.run", "notes": "fixture identity linker live apply"},
                )
                cur.execute("SELECT MAX(RUN_ID) FROM CAFC_DB.CORE.INGESTION_RUNS")
                run_id = int(cur.fetchone()[0])
                log.info("Opened INGESTION_RUNS.RUN_ID = %d", run_id)

            minted = mint_fixtures_bulk(cur, rows, run_id=run_id)

            if owns_envelope:
                cur.execute(
                    """
                    UPDATE CAFC_DB.CORE.INGESTION_RUNS
                       SET STATUS = 'SUCCESS', FINISHED_AT = CURRENT_TIMESTAMP()
                     WHERE RUN_ID = %(rid)s
                    """,
                    {"rid": run_id},
                )
                conn.commit()

            log.info("Applied (RUN_ID=%s): minted=%d", run_id, minted)
            return {"minted": minted, "skipped_missing_squad": skipped["missing_squad_id"],
                     "skipped_bad_date": skipped["unparseable_date"]}
    finally:
        conn.close()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--apply", action="store_true",
                    help="Actually write to Snowflake. Default is dry-run.")
    p.add_argument("--include-womens", action="store_true",
                    help="Override the platform-wide women's-competition exclusion and "
                         "link women's fixtures too. Off by default.")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging.")
    return p


if __name__ == "__main__":
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )
    run(dry_run=not args.apply, include_womens=args.include_womens)
