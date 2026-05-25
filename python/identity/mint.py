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
          (CAFC_PLAYER_ID, DISPLAY_NAME, BIRTH_DATE, CREATED_FROM_SOURCE)
        VALUES
          (%(cafc)s, %(name)s, %(dob)s, %(src)s)
        """,
        {
            "cafc": new_cafc_player_id,
            "name": display_name,
            "dob":  ext.source_birth_date,
            "src":  ext.source_system,
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
