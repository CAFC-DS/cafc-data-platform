"""
Write to CORE.PLAYER_IDENTITY_CANDIDATES — the ambiguous-match queue.

When the matcher sees multiple plausible CORE.PLAYERS rows for one external
identity it writes one row per candidate here and does NOT link. Humans
resolve by inserting into CORE.PLAYER_IDENTITY_OVERRIDES (which the matcher
will pick up on the next refresh) and updating STATUS here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from python.identity.matcher import NewExternalPlayer


log = logging.getLogger("cafc.identity.candidates")


def queue_candidates(
    cur,
    ext: "NewExternalPlayer",
    candidate_cafc_player_ids: Iterable[int],
    *,
    run_id: int,
    match_reason: str = "name+dob exact, multiple matches",
) -> int:
    """
    Insert one PENDING row per candidate.

    Returns the number of rows inserted. Idempotent against the same
    (source_system, source_player_id, candidate_cafc_player_id, status='PENDING')
    triple: re-running just no-ops if the candidate is already queued.
    """
    n = 0
    for cand in candidate_cafc_player_ids:
        cur.execute(
            """
            INSERT INTO CAFC_DB.CORE.PLAYER_IDENTITY_CANDIDATES
              (SOURCE_SYSTEM, SOURCE_PLAYER_ID, CANDIDATE_CAFC_PLAYER_ID,
               SOURCE_NAME, SOURCE_BIRTH_DATE, MATCH_REASON, RUN_ID, STATUS)
            SELECT %(src)s, %(ext_id)s, %(cand)s, %(name)s, %(dob)s, %(reason)s, %(rid)s, 'PENDING'
            WHERE NOT EXISTS (
                SELECT 1
                FROM CAFC_DB.CORE.PLAYER_IDENTITY_CANDIDATES q
                WHERE q.SOURCE_SYSTEM            = %(src)s
                  AND q.SOURCE_PLAYER_ID         = %(ext_id)s
                  AND q.CANDIDATE_CAFC_PLAYER_ID = %(cand)s
                  AND q.STATUS                   = 'PENDING'
            )
            """,
            {
                "src":    ext.source_system,
                "ext_id": ext.source_player_id,
                "cand":   cand,
                "name":   ext.source_name,
                "dob":    ext.source_birth_date,
                "reason": match_reason,
                "rid":    run_id,
            },
        )
        n += 1
    log.debug(
        "Queued %d candidate(s) for (%s, %s)",
        n, ext.source_system, ext.source_player_id,
    )
    return n
