"""
Manually merge two CAFC_PLAYER_IDs.

Plan §2.6 invariant 1: CAFC_PLAYER_ID is append-only. The matcher never
re-points an identity. When two CAFC_PLAYER_IDs turn out to be the same
human (one was minted because a match looked ambiguous; one was an
existing canonical player), a human runs this script to consolidate them.

What this does:
  1. Re-points every CORE.PLAYER_IDENTITIES row from <loser> to <winner>.
  2. Marks the loser's CORE.PLAYERS row IS_ACTIVE = FALSE so the row stays
     around (preserving audit trail and any FK references) but downstream
     models can filter it out.
  3. Records the merge in CORE.PLAYER_IDENTITY_OVERRIDES with a REASON
     prefix of "merged from <loser>". This means that if any new external
     identity is later linked to <loser> (shouldn't happen but defensive),
     the override will catch it and route to <winner>.

CLI is intentionally noisy and confirmation-gated. There is no --yes flag.

Usage:
    python -m python.identity.merge --loser 1234567 --winner 1234500 \\
        --reason "same player; loser was minted on 2026-04-12 because DOB was missing in IMPECT"
"""

from __future__ import annotations

import argparse
import logging
import sys

from python import _snowflake


log = logging.getLogger("cafc.identity.merge")


def merge_player(
    cur,
    *,
    loser_cafc_player_id: int,
    winner_cafc_player_id: int,
    reason: str,
    actor: str,
) -> None:
    """
    Caller drives the transaction. Three SQL statements, all idempotent
    against re-running (UPDATE moves nothing the second time, IS_ACTIVE
    UPDATE re-asserts FALSE, the INSERT into overrides uses NOT EXISTS).
    """
    if loser_cafc_player_id == winner_cafc_player_id:
        raise ValueError("loser and winner must differ")

    # 0. Fail fast on bad IDs. A typo'd --winner would otherwise re-point
    #    identities onto a non-existent CAFC_PLAYER_ID (orphaning them, caught
    #    only later by no_orphan_kpis), and merging into an already-retired
    #    player is almost always a mistake.
    cur.execute(
        """
        SELECT CAFC_PLAYER_ID, IS_ACTIVE
        FROM CAFC_DB.CORE.PLAYERS
        WHERE CAFC_PLAYER_ID IN (%(loser)s, %(winner)s)
        """,
        {"loser": loser_cafc_player_id, "winner": winner_cafc_player_id},
    )
    found = {int(r[0]): r[1] for r in cur.fetchall()}
    if winner_cafc_player_id not in found:
        raise ValueError(f"winner CAFC_PLAYER_ID={winner_cafc_player_id} not found in CORE.PLAYERS")
    if loser_cafc_player_id not in found:
        raise ValueError(f"loser CAFC_PLAYER_ID={loser_cafc_player_id} not found in CORE.PLAYERS")
    if found[winner_cafc_player_id] is False:
        raise ValueError(
            f"winner CAFC_PLAYER_ID={winner_cafc_player_id} is IS_ACTIVE=FALSE "
            "(already retired/merged) — pick an active winner"
        )

    # 1. Re-point identities. Capture which (source_system, source_player_id) pairs
    #    moved so we can write override rows for them.
    cur.execute(
        """
        SELECT SOURCE_SYSTEM, SOURCE_PLAYER_ID
        FROM CAFC_DB.CORE.PLAYER_IDENTITIES
        WHERE CAFC_PLAYER_ID = %(loser)s
        """,
        {"loser": loser_cafc_player_id},
    )
    moved_identities = list(cur.fetchall())

    cur.execute(
        """
        UPDATE CAFC_DB.CORE.PLAYER_IDENTITIES
           SET CAFC_PLAYER_ID = %(winner)s,
               UPDATED_AT     = CURRENT_TIMESTAMP()
         WHERE CAFC_PLAYER_ID = %(loser)s
        """,
        {"winner": winner_cafc_player_id, "loser": loser_cafc_player_id},
    )

    # 2. Soft-delete the loser canonical row.
    cur.execute(
        """
        UPDATE CAFC_DB.CORE.PLAYERS
           SET IS_ACTIVE = FALSE,
               UPDATED_AT = CURRENT_TIMESTAMP()
         WHERE CAFC_PLAYER_ID = %(loser)s
        """,
        {"loser": loser_cafc_player_id},
    )

    # 3. Insert override rows so future re-extracts can never re-land on the loser.
    full_reason = f"merged from {loser_cafc_player_id} to {winner_cafc_player_id}: {reason}"
    for source_system, source_player_id in moved_identities:
        cur.execute(
            """
            INSERT INTO CAFC_DB.CORE.PLAYER_IDENTITY_OVERRIDES
              (SOURCE_SYSTEM, SOURCE_PLAYER_ID, CAFC_PLAYER_ID, REASON, CREATED_BY)
            SELECT %(src)s, %(ext_id)s, %(winner)s, %(reason)s, %(actor)s
            WHERE NOT EXISTS (
                SELECT 1 FROM CAFC_DB.CORE.PLAYER_IDENTITY_OVERRIDES o
                 WHERE o.SOURCE_SYSTEM    = %(src)s
                   AND o.SOURCE_PLAYER_ID = %(ext_id)s
            )
            """,
            {
                "src":    source_system,
                "ext_id": source_player_id,
                "winner": winner_cafc_player_id,
                "reason": full_reason,
                "actor":  actor,
            },
        )

    log.info(
        "Merged CAFC_PLAYER_ID %d → %d; re-pointed %d identities; wrote %d override rows.",
        loser_cafc_player_id, winner_cafc_player_id,
        len(moved_identities), len(moved_identities),
    )


def _confirm(loser: int, winner: int, reason: str) -> bool:
    print()
    print("=" * 72)
    print(f"  MERGE   loser  CAFC_PLAYER_ID={loser}   →   winner CAFC_PLAYER_ID={winner}")
    print(f"  reason: {reason}")
    print("=" * 72)
    print("  This will:")
    print(f"    1. UPDATE every CORE.PLAYER_IDENTITIES row from {loser} → {winner}")
    print(f"    2. UPDATE CORE.PLAYERS.IS_ACTIVE = FALSE WHERE CAFC_PLAYER_ID = {loser}")
    print(f"    3. INSERT override rows so re-extracts never re-land on {loser}")
    print()
    answer = input("  Type 'merge' to confirm, anything else to abort: ").strip()
    return answer == "merge"


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--loser",  type=int, required=True,
                   help="CAFC_PLAYER_ID to retire.")
    p.add_argument("--winner", type=int, required=True,
                   help="CAFC_PLAYER_ID to consolidate identities under.")
    p.add_argument("--reason", required=True,
                   help="Human-readable justification — gets recorded in PLAYER_IDENTITY_OVERRIDES.REASON.")
    p.add_argument("--actor", default=None,
                   help="Override CREATED_BY (defaults to current Snowflake user).")
    return p


if __name__ == "__main__":
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    if not _confirm(args.loser, args.winner, args.reason):
        print("Aborted.")
        sys.exit(1)

    conn = _snowflake.get_connection()
    try:
        with conn.cursor() as cur:
            actor = args.actor
            if actor is None:
                cur.execute("SELECT CURRENT_USER()")
                actor = cur.fetchone()[0]
            merge_player(
                cur,
                loser_cafc_player_id=args.loser,
                winner_cafc_player_id=args.winner,
                reason=args.reason,
                actor=actor,
            )
        conn.commit()
        print("Merged.")
    except Exception:  # noqa: BLE001
        conn.rollback()
        log.exception("Merge failed — rolled back.")
        sys.exit(1)
    finally:
        conn.close()
