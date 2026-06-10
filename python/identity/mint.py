"""
Mint a new canonical player.

Allocates a new CAFC_PLAYER_ID from CORE.CAFC_PLAYER_ID_SEQ (the existing
Snowflake sequence already wired as the DEFAULT on CORE.PLAYERS), inserts
a CORE.PLAYERS row capturing what the external source told us, and links
the external identifier in CORE.PLAYER_IDENTITIES.

Plan §2.6 invariant 1: CAFC_PLAYER_ID is append-only. Nothing here ever
UPDATEs or re-points an existing identity.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from python.identity.matcher import NewExternalPlayer


log = logging.getLogger("cafc.identity.mint")


def mint_players_bulk(cur, exts: "list[NewExternalPlayer]") -> "list[int]":
    """
    Mint N new canonical players in a handful of round-trips instead of 3N.

    Allocates a block of CAFC_PLAYER_IDs from the sequence in one call
    (NEXTVAL over a GENERATOR), then batch-inserts the CORE.PLAYERS rows and
    their primary CORE.PLAYER_IDENTITIES rows via executemany. Returns the new
    CAFC_PLAYER_IDs aligned positionally to `exts`.

    Caller owns the surrounding transaction. Same invariants as mint_player:
    append-only IDs, IS_PRIMARY=TRUE on the minted player's identity row.
    """
    if not exts:
        return []

    # One sequence call yields N distinct values (sequences are monotonic, so
    # the block is unique and append-only). ROWCOUNT must be a literal, and
    # len() is a trusted int, so format it directly.
    cur.execute(
        "SELECT CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ.NEXTVAL "
        f"FROM TABLE(GENERATOR(ROWCOUNT => {len(exts)}))"
    )
    new_ids = [int(r[0]) for r in cur.fetchall()]
    if len(new_ids) != len(exts):
        raise RuntimeError(
            f"sequence block size mismatch: asked {len(exts)}, got {len(new_ids)}"
        )

    cur.executemany(
        """
        INSERT INTO CAFC_DB.CORE.PLAYERS
          (CAFC_PLAYER_ID, DISPLAY_NAME, COMMON_NAME, FIRST_NAME, LAST_NAME,
           BIRTH_DATE, CREATED_FROM_SOURCE)
        VALUES (%(cafc)s, %(name)s, %(common)s, %(first)s, %(last)s,
                %(dob)s, %(src)s)
        """,
        [
            {
                "cafc":   nid,
                "name":   e.source_name or f"unknown ({e.source_system}:{e.source_player_id})",
                "common": e.source_name or None,
                "first":  e.source_first_name or None,
                "last":   e.source_last_name or None,
                "dob":    e.source_birth_date,
                "src":    e.source_system,
            }
            for e, nid in zip(exts, new_ids)
        ],
    )

    cur.executemany(
        """
        INSERT INTO CAFC_DB.CORE.PLAYER_IDENTITIES
          (CAFC_PLAYER_ID, SOURCE_SYSTEM, SOURCE_PLAYER_ID,
           SOURCE_NAME, SOURCE_BIRTH_DATE, MATCH_CONFIDENCE, IS_PRIMARY)
        VALUES (%(cafc)s, %(src)s, %(ext_id)s, %(name)s, %(dob)s, 100, TRUE)
        """,
        [
            {
                "cafc":   nid,
                "src":    e.source_system,
                "ext_id": e.source_player_id,
                "name":   e.source_name,
                "dob":    e.source_birth_date,
            }
            for e, nid in zip(exts, new_ids)
        ],
    )

    log.debug("Bulk-minted %d players (CAFC_PLAYER_ID %d..%d)",
              len(new_ids), min(new_ids), max(new_ids))
    return new_ids


def mint_player(cur, ext: "NewExternalPlayer") -> int:
    """
    Mint a new CAFC_PLAYER_ID and link `ext` to it.

    Returns the freshly-minted CAFC_PLAYER_ID.

    Caller is responsible for the surrounding transaction — this function
    does two INSERTs and they must commit together with the rest of the
    matcher's work (or roll back together on failure).
    """
    cur.execute("SELECT CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ.NEXTVAL")
    new_cafc_player_id = int(cur.fetchone()[0])

    log.debug(
        "Minting CAFC_PLAYER_ID=%d for (%s, %s) name=%r dob=%s",
        new_cafc_player_id, ext.source_system, ext.source_player_id,
        ext.source_name, ext.source_birth_date,
    )

    # CORE.PLAYERS.DISPLAY_NAME is NOT NULL; fall back to source_player_id if
    # the source somehow gave us a blank name (shouldn't happen in IMPECT data
    # but the constraint matters).
    display_name = ext.source_name or f"unknown ({ext.source_system}:{ext.source_player_id})"

    cur.execute(
        """
        INSERT INTO CAFC_DB.CORE.PLAYERS
          (CAFC_PLAYER_ID, DISPLAY_NAME, COMMON_NAME, FIRST_NAME, LAST_NAME,
           BIRTH_DATE, CREATED_FROM_SOURCE)
        VALUES
          (%(cafc)s, %(name)s, %(common)s, %(first)s, %(last)s, %(dob)s, %(src)s)
        """,
        {
            "cafc":   new_cafc_player_id,
            "name":   display_name,
            "common": ext.source_name or None,
            "first":  ext.source_first_name or None,
            "last":   ext.source_last_name or None,
            "dob":    ext.source_birth_date,
            "src":    ext.source_system,
        },
    )

    cur.execute(
        """
        INSERT INTO CAFC_DB.CORE.PLAYER_IDENTITIES
          (CAFC_PLAYER_ID, SOURCE_SYSTEM, SOURCE_PLAYER_ID,
           SOURCE_NAME, SOURCE_BIRTH_DATE, MATCH_CONFIDENCE, IS_PRIMARY)
        VALUES
          (%(cafc)s, %(src)s, %(ext_id)s, %(name)s, %(dob)s, 100, TRUE)
        """,
        {
            "cafc":   new_cafc_player_id,
            "src":    ext.source_system,
            "ext_id": ext.source_player_id,
            "name":   ext.source_name,
            "dob":    ext.source_birth_date,
        },
    )

    return new_cafc_player_id
