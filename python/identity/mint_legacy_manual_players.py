"""
Backfill: mint legacy manually-added players that the June 2026 migration
missed into the canonical layer.

The June migration created CORE.PLAYERS + a MANUAL CORE.PLAYER_IDENTITIES row
(SOURCE_PLAYER_ID = the legacy CAFC_PLAYER_ID as text) for ~55 of the 99
internal players in RECRUITMENT_TEST.PUBLIC.PLAYERS. It missed 58 — youth /
academy / lower-league players a scout added by hand because IMPECT doesn't
cover them. Their list items, scout reports and stage-history rows resolve
fine on legacy but show "Unknown Player" after the canonical cutover, because
their legacy CAFC id (small, e.g. 11301) has no row in CORE.PLAYERS (canonical
ids are 1.4M+) and doesn't collide meaningfully with any IMPECT id.

This mints them the same way, then the phase-5 remap
(20260611_phase5_remap_legacy_player_ids.sql) repoints the app rows.

    .venv/bin/python -m python.identity.mint_legacy_manual_players           # dry-run
    .venv/bin/python -m python.identity.mint_legacy_manual_players --apply

Idempotent: a legacy internal player that already has a MANUAL identity is
skipped, so re-running after a partial failure is safe.
"""
from __future__ import annotations

import argparse
import logging

from python import _snowflake

log = logging.getLogger("cafc.identity.mint_legacy_manual_players")

FIND_MISSING = """
    SELECT lp.CAFC_PLAYER_ID, lp.PLAYERNAME, lp.BIRTHDATE
    FROM RECRUITMENT_TEST.PUBLIC.PLAYERS lp
    WHERE lp.PLAYERID IS NULL
      AND lp.CAFC_PLAYER_ID IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM CAFC_DB.CORE.PLAYER_IDENTITIES pi
          WHERE pi.SOURCE_SYSTEM = 'MANUAL'
            AND TRY_TO_NUMBER(pi.SOURCE_PLAYER_ID) = lp.CAFC_PLAYER_ID
      )
    ORDER BY lp.CAFC_PLAYER_ID
"""


def split_name(full: str) -> tuple[str, str | None]:
    parts = [p for p in (full or "").strip().split() if p]
    if not parts:
        return (full or "").strip(), None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def run(dry_run: bool = True) -> dict[str, int]:
    conn = _snowflake.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("USE ROLE DEV_ROLE")
        cur.execute("USE WAREHOUSE DEVELOPMENT_WH")

        cur.execute(FIND_MISSING)
        missing = cur.fetchall()
        log.info("%d legacy internal players missing a canonical identity", len(missing))

        if dry_run:
            print()
            print("=" * 72)
            print("  DRY RUN — no writes")
            print("=" * 72)
            for legacy_cafc, name, dob in missing[:60]:
                fn, ln = split_name(name)
                print(f"  legacy_cafc={legacy_cafc:<7} {name!r:<38} dob={dob}  -> ({fn!r}, {ln!r})")
            print(f"\n  would mint: {len(missing)} CORE.PLAYERS + {len(missing)} MANUAL identities")
            return {"would_mint": len(missing)}

        if not missing:
            return {"minted": 0}

        # Mint a block of canonical ids in one call.
        n = len(missing)
        cur.execute(
            "SELECT CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ.NEXTVAL FROM TABLE(GENERATOR(ROWCOUNT => %(n)s))",
            {"n": n},
        )
        new_ids = sorted(r[0] for r in cur.fetchall())
        assert len(new_ids) == n, f"seq gave {len(new_ids)} for {n} rows"

        player_rows, identity_rows = [], []
        for cafc_id, (legacy_cafc, name, dob) in zip(new_ids, missing):
            fn, ln = split_name(name)
            player_rows.append({
                "cafc": cafc_id, "display": (name or "").strip(),
                "first": fn, "last": ln, "dob": dob,
            })
            identity_rows.append({
                "cafc": cafc_id, "src": str(legacy_cafc),
                "name": (name or "").strip(), "dob": dob,
            })

        cur.executemany(
            """
            INSERT INTO CAFC_DB.CORE.PLAYERS
              (CAFC_PLAYER_ID, DISPLAY_NAME, FIRST_NAME, LAST_NAME, BIRTH_DATE,
               CREATED_FROM_SOURCE, IS_ACTIVE)
            VALUES (%(cafc)s, %(display)s, %(first)s, %(last)s, %(dob)s, 'MANUAL', TRUE)
            """,
            player_rows,
        )
        cur.executemany(
            """
            INSERT INTO CAFC_DB.CORE.PLAYER_IDENTITIES
              (CAFC_PLAYER_ID, SOURCE_SYSTEM, SOURCE_PLAYER_ID, SOURCE_NAME,
               SOURCE_BIRTH_DATE, MATCH_CONFIDENCE, IS_PRIMARY)
            VALUES (%(cafc)s, 'MANUAL', %(src)s, %(name)s, %(dob)s, 100, TRUE)
            """,
            identity_rows,
        )
        conn.commit()
        log.info("minted %d canonical players + MANUAL identities", n)
        print(f"\n  minted {n}. Now re-run 20260611_phase5_remap_legacy_player_ids.sql "
              f"and `dbt build --select tag:app_compat --target prod`.")
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
