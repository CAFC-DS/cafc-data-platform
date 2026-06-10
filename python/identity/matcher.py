"""
Player identity matcher.

Pulls every (SOURCE_SYSTEM, SOURCE_PLAYER_ID) pair from an external source
that isn't yet linked in CORE.PLAYER_IDENTITIES, then for each one decides
between four outcomes:

    OVERRIDE       a row exists in CORE.PLAYER_IDENTITY_OVERRIDES,
                   use that CAFC_PLAYER_ID unconditionally.
    LINK_EXISTING  exactly one CORE.PLAYERS row matches by
                   (normalized common_name, birth_date), link to it.
    MINT_NEW       no CORE.PLAYERS row matches, mint a fresh
                   CAFC_PLAYER_ID via CORE.CAFC_PLAYER_ID_SEQ.
    AMBIGUOUS      multiple CORE.PLAYERS rows match; do NOT link.
                   Write one row per candidate to
                   CORE.PLAYER_IDENTITY_CANDIDATES and leave the
                   external player un-linked until a human resolves it.

Three invariants enforced by construction (plan §2.6):

  1. CAFC_PLAYER_ID is append-only. The matcher never UPDATEs an
     existing identity. Re-aliasing two existing CAFC_PLAYER_IDs is
     python/identity/merge.py and is run by a human, not by the loop.
  2. Override table always wins. If a (SOURCE_SYSTEM, SOURCE_PLAYER_ID)
     pair has a row in PLAYER_IDENTITY_OVERRIDES the matcher copies
     that decision rather than re-computing.
  3. Ambiguity is a non-result. The matcher never guesses between two
     candidates; downstream dbt tests fail the refresh if any KPI rows
     reference a still-queued player.

The whole apply phase runs inside one Snowflake transaction tied to the
current CORE.INGESTION_RUNS.RUN_ID, so a mid-flight failure leaves no
half-linked state.

Run with --dry-run for a read-only classification report; the default
(no --dry-run) commits writes.
"""

from __future__ import annotations

import argparse
import logging
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Literal, Optional

from python import _snowflake
from python.identity import candidates as candidates_mod
from python.identity import mint as mint_mod


log = logging.getLogger("cafc.identity.matcher")

Outcome = Literal["OVERRIDE", "LINK_EXISTING", "MINT_NEW", "AMBIGUOUS"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewExternalPlayer:
    source_system: str          # 'IMPECT', 'MANUAL', etc.
    source_player_id: str
    source_name: str            # provider common name — the matching key
    source_birth_date: Optional[date]
    source_first_name: str = ""
    source_last_name: str = ""


@dataclass(frozen=True)
class ExistingPlayer:
    cafc_player_id: int
    common_name: str
    birth_date: Optional[date]


@dataclass
class MatchDecision:
    external: NewExternalPlayer
    outcome: Outcome
    cafc_player_id: Optional[int] = None     # set for OVERRIDE / LINK_EXISTING / MINT_NEW
    candidate_ids: list[int] = field(default_factory=list)
    reason: str = ""


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

def normalize_name(name: Optional[str]) -> str:
    """
    Lowercase, strip accents, drop everything that isn't a letter or digit.

    Matches "Müller" with "muller", "O'Brien" with "obrien", "St. John"
    with "stjohn". Conservative on purpose: the matcher only links on an
    exact normalized match, so over-normalizing (e.g. dropping all spaces
    so "AlMallah" matches "Al Mallah") errs on the side of false-positives
    and would route real distinct people through LINK_EXISTING instead of
    AMBIGUOUS. The current shape errs the other way: collisions that look
    "obviously the same person" to a human will be AMBIGUOUS if any
    punctuation differs, which is the safer failure mode.
    """
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    no_marks = "".join(c for c in nfd if not unicodedata.combining(c))
    return "".join(c.lower() for c in no_marks if c.isalnum())


# ---------------------------------------------------------------------------
# Snowflake reads
# ---------------------------------------------------------------------------

def fetch_new_external_players(
    cur, source_system: str, include_womens: bool = False
) -> list[NewExternalPlayer]:
    """
    Every distinct (source_system, source_player_id) pair from IMPECT_RAW.PLAYERS
    that isn't already in CORE.PLAYER_IDENTITIES.

    Two correctness guards specific to IMPECT_RAW.PLAYERS:

      - It is loaded one row per (player, iteration) appearance, so it is NOT
        unique on ID. We dedup to one row per ID (keeping the newest
        iteration's attributes via QUALIFY ROW_NUMBER) — otherwise a player
        appearing in N still-unlinked iterations would be classified N times
        and minted N times, producing N distinct CAFC_PLAYER_IDs for one human
        (this is the likely origin of the existing handful of source_player_ids
        that map to >1 CAFC_PLAYER_ID).

      - Women's competitions are excluded platform-wide (see the loaders'
        --include-womens and dbt's include_womens_competitions var), so FEMALE
        players are skipped unless include_womens=True. Without this the matcher
        would mint canonical IDs for players we deliberately keep out of the
        platform.

    Sourcing from raw IMPECT_RAW.PLAYERS rather than stg_impect__players because
    the staging schema name is dbt-target-dependent (IMPECT_RAW_STAGING in prod,
    IMPECT_RAW_STAGING_DEV_<user> in dev) and this Python code has no target
    context; the gender filter here reproduces staging's exclusion directly.
    """
    gender_clause = "" if include_womens else "AND r.GENDER = 'MALE'"
    cur.execute(
        f"""
        SELECT
            r.ID                          AS source_player_id,
            r.COMMONNAME                  AS source_name,
            TRY_TO_DATE(r.BIRTHDATE)      AS source_birth_date,
            r.FIRSTNAME                   AS source_first_name,
            r.LASTNAME                    AS source_last_name
        FROM CAFC_DB.IMPECT_RAW.PLAYERS r
        LEFT JOIN CAFC_DB.CORE.PLAYER_IDENTITIES pi
          ON  pi.SOURCE_SYSTEM    = %(src)s
          AND pi.SOURCE_PLAYER_ID = r.ID::VARCHAR
        WHERE pi.PLAYER_IDENTITY_ID IS NULL
          {gender_clause}
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY r.ID ORDER BY r.ITERATION_ID DESC NULLS LAST
        ) = 1
        """,
        {"src": source_system},
    )
    return [
        NewExternalPlayer(
            source_system=source_system,
            source_player_id=str(row[0]),
            source_name=row[1] or "",
            source_birth_date=row[2],
            source_first_name=row[3] or "",
            source_last_name=row[4] or "",
        )
        for row in cur.fetchall()
    ]


def fetch_overrides(cur, source_system: str) -> dict[str, int]:
    """
    Map source_player_id → cafc_player_id from CORE.PLAYER_IDENTITY_OVERRIDES.

    Returns an empty dict if the override table doesn't exist yet (e.g.
    snowflake/ddl/20260525_identity_overrides.sql hasn't been applied),
    so dry-runs work even before DDL is in place.
    """
    try:
        cur.execute(
            """
            SELECT SOURCE_PLAYER_ID, CAFC_PLAYER_ID
            FROM CAFC_DB.CORE.PLAYER_IDENTITY_OVERRIDES
            WHERE SOURCE_SYSTEM = %(src)s
            """,
            {"src": source_system},
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        log.warning(
            "PLAYER_IDENTITY_OVERRIDES not readable (%s) — treating as empty. "
            "Apply snowflake/ddl/20260525_identity_overrides.sql to enable overrides.",
            type(e).__name__,
        )
        return {}


def fetch_existing_players(cur) -> list[ExistingPlayer]:
    """Every CORE.PLAYERS row, materialized once into Python for in-memory lookup."""
    cur.execute(
        """
        SELECT CAFC_PLAYER_ID, COALESCE(DISPLAY_NAME, FIRST_NAME || ' ' || LAST_NAME),
               BIRTH_DATE
        FROM CAFC_DB.CORE.PLAYERS
        """
    )
    return [
        ExistingPlayer(
            cafc_player_id=int(row[0]),
            common_name=row[1] or "",
            birth_date=row[2],
        )
        for row in cur.fetchall()
    ]


def build_lookup_index(
    existing: Iterable[ExistingPlayer],
) -> dict[tuple[str, Optional[date]], list[int]]:
    """Group existing players by (normalized_name, birth_date) → list of CAFC_PLAYER_IDs."""
    idx: dict[tuple[str, Optional[date]], list[int]] = defaultdict(list)
    for p in existing:
        key = (normalize_name(p.common_name), p.birth_date)
        idx[key].append(p.cafc_player_id)
    return idx


# ---------------------------------------------------------------------------
# Classification (the actual algorithm)
# ---------------------------------------------------------------------------

def classify_one(
    ext: NewExternalPlayer,
    overrides: dict[str, int],
    lookup: dict[tuple[str, Optional[date]], list[int]],
) -> MatchDecision:
    # 1. Override always wins.
    override_target = overrides.get(ext.source_player_id)
    if override_target is not None:
        return MatchDecision(
            external=ext,
            outcome="OVERRIDE",
            cafc_player_id=override_target,
            reason=f"PLAYER_IDENTITY_OVERRIDES row routes {ext.source_player_id} → {override_target}",
        )

    # 2. Name+DOB lookup against CORE.PLAYERS.
    key = (normalize_name(ext.source_name), ext.source_birth_date)
    matches = lookup.get(key, [])

    if len(matches) == 1:
        return MatchDecision(
            external=ext,
            outcome="LINK_EXISTING",
            cafc_player_id=matches[0],
            candidate_ids=matches,
            reason="exact match on (normalized common_name, birth_date)",
        )
    if len(matches) == 0:
        return MatchDecision(
            external=ext,
            outcome="MINT_NEW",
            reason="no existing CORE.PLAYERS row matched (normalized common_name, birth_date)",
        )
    return MatchDecision(
        external=ext,
        outcome="AMBIGUOUS",
        candidate_ids=matches,
        reason=f"{len(matches)} existing CORE.PLAYERS rows matched (normalized common_name, birth_date)",
    )


def classify_all(
    externals: Iterable[NewExternalPlayer],
    overrides: dict[str, int],
    lookup: dict[tuple[str, Optional[date]], list[int]],
) -> list[MatchDecision]:
    return [classify_one(e, overrides, lookup) for e in externals]


# ---------------------------------------------------------------------------
# Apply phase (write to Snowflake under one transaction).
# ---------------------------------------------------------------------------

def apply_decisions(cur, decisions: list[MatchDecision], run_id: int) -> dict[str, int]:
    """
    Persist matcher decisions in batches. Caller is responsible for transaction
    management and for having opened a CORE.INGESTION_RUNS row to supply run_id.
    Returns per-outcome counts.

    Batched rather than row-by-row: links go in one executemany, mints go
    through mint_players_bulk (one sequence-block allocation + two
    executemany inserts), candidates are queued last. This collapses what was
    ~3N round-trips into a small constant number — a full-base backfill drops
    from tens of minutes to seconds.

    Does NOT call commit() — the caller drives the transaction so that upstream
    extractor writes and matcher writes commit together.
    """
    counts = {"OVERRIDE": 0, "LINK_EXISTING": 0, "MINT_NEW": 0, "AMBIGUOUS": 0}
    links: list[MatchDecision] = []
    mints: list[MatchDecision] = []
    ambiguous: list[MatchDecision] = []

    for d in decisions:
        counts[d.outcome] += 1
        if d.outcome in ("OVERRIDE", "LINK_EXISTING"):
            links.append(d)
        elif d.outcome == "MINT_NEW":
            mints.append(d)
        elif d.outcome == "AMBIGUOUS":
            ambiguous.append(d)

    # Links: attach a new external pair to an already-canonical player, so
    # IS_PRIMARY=FALSE (the existing player already owns its primary row).
    if links:
        _insert_identity_links(cur, links)

    # Mints: allocate a sequence block + batch-insert PLAYERS and their primary
    # identity rows; backfill the assigned ids onto the decisions.
    if mints:
        new_ids = mint_mod.mint_players_bulk(cur, [d.external for d in mints])
        for d, nid in zip(mints, new_ids):
            d.cafc_player_id = nid

    # Ambiguous: queued for human review (typically few; left row-by-row for
    # its idempotent NOT-EXISTS guard).
    for d in ambiguous:
        candidates_mod.queue_candidates(cur, d.external, d.candidate_ids, run_id=run_id)

    return counts


def _insert_identity_links(cur, links: list[MatchDecision]) -> None:
    """Batch-insert CORE.PLAYER_IDENTITIES rows for OVERRIDE / LINK_EXISTING
    decisions (IS_PRIMARY=FALSE; confidence 100 — override is a human decision,
    link is an exact normalized match)."""
    cur.executemany(
        """
        INSERT INTO CAFC_DB.CORE.PLAYER_IDENTITIES
          (CAFC_PLAYER_ID, SOURCE_SYSTEM, SOURCE_PLAYER_ID,
           SOURCE_NAME, SOURCE_BIRTH_DATE, MATCH_CONFIDENCE, IS_PRIMARY)
        VALUES
          (%(cafc)s, %(src)s, %(ext_id)s, %(name)s, %(dob)s, 100, FALSE)
        """,
        [
            {
                "cafc":   d.cafc_player_id,
                "src":    d.external.source_system,
                "ext_id": d.external.source_player_id,
                "name":   d.external.source_name,
                "dob":    d.external.source_birth_date,
            }
            for d in links
        ],
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(
    source_system: str = "IMPECT",
    dry_run: bool = True,
    run_id: Optional[int] = None,
    include_womens: bool = False,
) -> dict[str, int]:
    """
    Entry point. Returns per-outcome counts.

    dry_run=True: read everything, classify, print a report, write nothing.
    dry_run=False, run_id=None:
        Open our own CORE.INGESTION_RUNS row, apply, mark SUCCESS, commit.
        Used when matcher.py is invoked standalone via the CLI.
    dry_run=False, run_id given:
        Apply inside the caller's envelope. Caller (typically the
        orchestrator) is responsible for opening and closing the run row.
    """
    # Normalize to the canonical uppercase form. SOURCE_SYSTEM is stored
    # uppercase ('IMPECT', 'MANUAL') and every Snowflake string comparison here
    # is case-sensitive, so a lowercase --source would match no existing
    # identities and treat the entire player base as new (re-minting everyone).
    source_system = source_system.strip().upper()

    conn = _snowflake.get_connection()
    try:
        with conn.cursor() as cur:
            log.info("Fetching new external players for %s…", source_system)
            externals = fetch_new_external_players(cur, source_system, include_womens=include_womens)
            log.info("  %d new (source_system, source_player_id) pairs to classify%s.",
                     len(externals), "" if include_womens else " (women's excluded)")

            overrides = fetch_overrides(cur, source_system)
            log.info("  %d override rows loaded.", len(overrides))

            existing = fetch_existing_players(cur)
            log.info("  %d existing CORE.PLAYERS rows loaded for lookup.", len(existing))

            lookup = build_lookup_index(existing)

            decisions = classify_all(externals, overrides, lookup)
            counts = _summarize(decisions)

            if dry_run:
                _print_dry_run_report(decisions, counts)
                return counts

            # Live apply path. Two modes:
            #   - run_id passed in: write inside caller's envelope; caller
            #     handles open + close + commit.
            #   - run_id is None: matcher owns the envelope.
            owns_envelope = run_id is None

            if owns_envelope:
                cur.execute(
                    """
                    INSERT INTO CAFC_DB.CORE.INGESTION_RUNS (SOURCE_SYSTEM, TRIGGERED_BY, NOTES)
                    VALUES (%(src)s, %(by)s, %(notes)s)
                    """,
                    {"src": source_system, "by": "matcher.run",
                     "notes": "identity matcher live apply"},
                )
                cur.execute("SELECT MAX(RUN_ID) FROM CAFC_DB.CORE.INGESTION_RUNS")
                run_id = int(cur.fetchone()[0])
                log.info("Opened INGESTION_RUNS.RUN_ID = %d", run_id)

            applied = apply_decisions(cur, decisions, run_id=run_id)

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

            log.info("Applied (RUN_ID=%d): %s", run_id, applied)
            return applied
    finally:
        conn.close()


def _summarize(decisions: list[MatchDecision]) -> dict[str, int]:
    out = {"OVERRIDE": 0, "LINK_EXISTING": 0, "MINT_NEW": 0, "AMBIGUOUS": 0}
    for d in decisions:
        out[d.outcome] += 1
    return out


def _print_dry_run_report(decisions: list[MatchDecision], counts: dict[str, int]) -> None:
    print()
    print("=" * 72)
    print("  DRY RUN — no writes performed")
    print("=" * 72)
    total = sum(counts.values())
    print(f"  Total external players to classify: {total}")
    for outcome, n in counts.items():
        pct = (n / total * 100) if total else 0.0
        print(f"    {outcome:<14s} {n:>6d}   ({pct:5.1f}%)")
    print()

    # Show a sample of each non-trivial outcome.
    samples = {"AMBIGUOUS": [], "MINT_NEW": [], "OVERRIDE": []}
    for d in decisions:
        if d.outcome in samples and len(samples[d.outcome]) < 5:
            samples[d.outcome].append(d)
    for outcome, ds in samples.items():
        if not ds:
            continue
        print(f"  -- sample {outcome} (up to 5) --")
        for d in ds:
            e = d.external
            print(
                f"    [{e.source_system}:{e.source_player_id:>8}] "
                f"name={e.source_name!r:<35} dob={e.source_birth_date} "
                f"→ {d.outcome}"
                + (f"  cafc={d.cafc_player_id}" if d.cafc_player_id else "")
                + (f"  candidates={d.candidate_ids}" if d.candidate_ids and d.outcome == "AMBIGUOUS" else "")
            )
        print()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--source", default="IMPECT",
                   help="SOURCE_SYSTEM to match (default: IMPECT).")
    p.add_argument("--apply", action="store_true",
                   help="Actually write to Snowflake. Default is dry-run.")
    p.add_argument("--include-womens", action="store_true",
                   help="Override the platform-wide women's-competition exclusion and "
                        "classify/mint women's players too. Off by default.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable DEBUG logging.")
    return p


if __name__ == "__main__":
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )
    try:
        run(source_system=args.source, dry_run=not args.apply,
            include_womens=args.include_womens)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
