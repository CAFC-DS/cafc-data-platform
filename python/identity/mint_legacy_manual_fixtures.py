"""
Backfill: mint legacy manually-added fixtures that never reached the canonical
layer into CORE.FIXTURES / CORE.FIXTURE_IDENTITIES.

Scouts add fixtures by hand in the recruitment app (youth games, trialist XIs,
internationals IMPECT doesn't cover). On legacy these land in
RECRUITMENT_TEST.PUBLIC.MATCHES with DATA_SOURCE='internal' and a small
CAFC_MATCH_ID. The June 2026 canonical build minted MANUAL
CORE.FIXTURE_IDENTITIES rows for ~203 of them; every internal match created
since (62 as of 2026-09-07) has no canonical identity at all, so after the
2026-09-06 cutover its scout reports show Fixture = "N/A" (APP_COMPAT.MATCHES
is built purely from CORE.FIXTURES).

This mints them the same way python.identity.mint_legacy_manual_players does
for players. The companion remap (20260907_remap_legacy_manual_fixture_ids.sql)
then repoints SCOUT_REPORTS.MATCH_ID off the legacy id onto the new canonical
CAFC_FIXTURE_ID.

Squad ids are carried through from legacy exactly as-is (NULL where legacy is
NULL) -- unlike the June build we do NOT invent 9000000-range placeholders.
Team names are copied into the new SOURCE_HOME_SQUAD_NAME / SOURCE_AWAY_SQUAD_NAME
columns (run 20260907_fixture_identities_add_source_squad_names.sql first) so
APP_COMPAT.MATCHES can render a name even when the squad id doesn't resolve.

Women's competitions: not filtered here. These rows were entered by hand and
carry scout reports; the platform-wide women's exclusion targets bulk IMPECT
ingestion, and gender can't be derived anyway (most of these squad ids aren't
in CORE_SQUADS). Left in deliberately.

    .venv/bin/python -m python.identity.mint_legacy_manual_fixtures           # dry-run
    .venv/bin/python -m python.identity.mint_legacy_manual_fixtures --apply

Idempotent: an internal match that already has a MANUAL identity (by
SOURCE_FIXTURE_ID = its legacy CAFC_MATCH_ID) is skipped, so re-running after
a partial failure is safe.
"""
from __future__ import annotations

import argparse
import logging

from python import _snowflake

log = logging.getLogger("cafc.identity.mint_legacy_manual_fixtures")

FIND_MISSING = """
    SELECT
        lm.CAFC_MATCH_ID,
        lm.HOMESQUADID,
        lm.AWAYSQUADID,
        lm.SCHEDULEDDATE AS SCHED,
        lm.HOMESQUADNAME,
        lm.AWAYSQUADNAME
    FROM RECRUITMENT_TEST.PUBLIC.MATCHES lm
    WHERE lm.DATA_SOURCE = 'internal'
      AND lm.CAFC_MATCH_ID IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM CAFC_DB.CORE.FIXTURE_IDENTITIES fi
          WHERE fi.SOURCE_SYSTEM = 'MANUAL'
            AND TRY_TO_NUMBER(fi.SOURCE_FIXTURE_ID) = lm.CAFC_MATCH_ID
      )
    ORDER BY lm.CAFC_MATCH_ID
"""


def run(dry_run: bool = True) -> dict[str, int]:
    conn = _snowflake.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("USE ROLE DEV_ROLE")
        cur.execute("USE WAREHOUSE DEVELOPMENT_WH")

        cur.execute(FIND_MISSING)
        missing = cur.fetchall()
        log.info("%d legacy internal fixtures missing a canonical identity", len(missing))

        if dry_run:
            print()
            print("=" * 78)
            print("  DRY RUN - no writes")
            print("=" * 78)
            for legacy_id, hs, aws, sched, hn, an in missing[:80]:
                print(f"  legacy={legacy_id:<7} {str(hn)!r:<32} vs {str(an)!r:<32} "
                      f"date={sched}  squad_ids=({hs},{aws})")
            null_dates = sum(1 for r in missing if r[3] is None)
            print(f"\n  would mint: {len(missing)} CORE.FIXTURES + {len(missing)} MANUAL identities")
            if null_dates:
                print(f"  ({null_dates} have an unparseable SCHEDULEDDATE -> FIXTURE_DATE NULL)")
            return {"would_mint": len(missing)}

        if not missing:
            return {"minted": 0}

        n = len(missing)
        cur.execute(
            "SELECT CAFC_DB.CORE.CAFC_FIXTURE_ID_SEQ.NEXTVAL FROM TABLE(GENERATOR(ROWCOUNT => %(n)s))",
            {"n": n},
        )
        new_ids = sorted(r[0] for r in cur.fetchall())
        assert len(new_ids) == n, f"seq gave {len(new_ids)} for {n} rows"

        fixture_rows, identity_rows = [], []
        for cafc_id, (legacy_id, hs, aws, sched, hn, an) in zip(new_ids, missing):
            fixture_rows.append({
                "cafc": cafc_id, "hs": hs, "aws": aws, "date": sched,
            })
            identity_rows.append({
                "cafc": cafc_id, "src": str(legacy_id),
                "hs": None if hs is None else str(hs),
                "aws": None if aws is None else str(aws),
                "date": sched, "hn": hn, "an": an,
            })

        cur.executemany(
            """
            INSERT INTO CAFC_DB.CORE.FIXTURES
              (CAFC_FIXTURE_ID, HOME_SQUAD_ID, AWAY_SQUAD_ID, FIXTURE_DATE,
               CREATED_FROM_SOURCE, IS_ACTIVE)
            VALUES (%(cafc)s, %(hs)s, %(aws)s, %(date)s, 'MANUAL', TRUE)
            """,
            fixture_rows,
        )
        cur.executemany(
            """
            INSERT INTO CAFC_DB.CORE.FIXTURE_IDENTITIES
              (CAFC_FIXTURE_ID, SOURCE_SYSTEM, SOURCE_FIXTURE_ID, SOURCE_CONTEXT,
               SOURCE_HOME_SQUAD_ID, SOURCE_AWAY_SQUAD_ID, SOURCE_FIXTURE_DATE,
               SOURCE_HOME_SQUAD_NAME, SOURCE_AWAY_SQUAD_NAME,
               MATCH_CONFIDENCE, IS_PRIMARY)
            VALUES (%(cafc)s, 'MANUAL', %(src)s, 'mint_legacy_manual_fixtures.py',
                    %(hs)s, %(aws)s, %(date)s, %(hn)s, %(an)s, 100, TRUE)
            """,
            identity_rows,
        )
        conn.commit()
        log.info("minted %d canonical fixtures + MANUAL identities", n)
        print(f"\n  minted {n}. Now run 20260907_backfill_manual_fixture_squads.sql, "
              f"20260907_remap_legacy_manual_fixture_ids.sql, then "
              f"`dbt build --select app_compat.matches --target prod`.")
        return {"minted": n}
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true", help="Write to Snowflake. Default is dry-run.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
    run(dry_run=not args.apply)
