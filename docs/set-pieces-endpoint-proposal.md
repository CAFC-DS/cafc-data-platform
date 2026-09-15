# Ingest IMPECT's `/set-pieces` endpoint — proposal + handover

**Purpose of this document**: hand this to whoever picks up work in
`cafc-data-platform` (a fresh Claude Code session or a human engineer) so
they can implement this without re-deriving the investigation. Everything
below was verified against the live IMPECT API on 2026-09-15 using the
credentials already in `python/extract/impect/.env` — sample values are
real, not illustrative.

## 1. Why this exists

`pre-match-set-piece-report` (and its sibling set-piece report repos) build
near/central/far-post corner classification, free-kick delivery type, swing
direction, and first/second-touch identity by **deriving** them from
`CAFC_DB.IMPECT_RAW.EVENTS` — because that table's `SET_PIECE_DETAIL` field
on each event is only `{id, mainEvent, subPhaseId}`, with no classification
data at all. The derivation (`pre_match_report/cafcdb_source.py` in that
repo) is a landing-position geometry heuristic, documented as an
approximation with known accuracy limits (~73% correlation with the legacy
table's near-post labels, ~50% i.e. chance-level for far-post), and swing
direction is flagged everywhere as "not in the feed, never inferred."

Investigating a user complaint that a generated Cardiff City report's corner
numbers looked wrong (2026-09-15), we found the derived near/central/far-post
distribution diverges sharply from IMPECT's own analysis-tool dashboard for
the same games. Chasing a better geometry heuristic to match one aggregate
screenshot would have been overfitting with no ground truth to check against.

Instead: **IMPECT has a dedicated endpoint that returns the real
classification directly**, and nothing in this codebase currently calls it.

## 2. What the endpoint actually returns (verified live)

```
GET /v5/customerapi/matches/{matchId}/set-pieces
```

Auth: same OAuth2 client-credentials flow every other `impect_api.py`
function already uses (`get_auth_token()`). No new credential or scope was
needed — it worked immediately with the existing `IMPECT_USERNAME`/
`IMPECT_PASSWORD` in `python/extract/impect/.env`.

Verified against match 267839 (Cardiff City vs AFC Wrexham, Championship
26/27): returns `{"data": [...]}`, a list of **83 set-piece phases** for
that match. Each phase:

```json
{
  "id": 90147817,
  "matchId": 267839,
  "startTime": "05:29.6640",
  "startTimeInSec": 329.664,
  "endTime": "06:01.7409",
  "endTimeInSec": 361.7409,
  "squadId": 2164,
  "phaseIndex": 6,
  "setPieceCategory": "CORNER_LEFT",
  "adjSetPieceCategory": "CORNER_LEFT",
  "setPieceExecutionType": "DIRECT",
  "setPieceSubPhase": [
    {
      "id": 95597453,
      "index": 0,
      "startZone": "LEFT_CORNER",
      "cornerEndZone": "NEAR_POST_CLOSE",
      "cornerType": "CORNER_NEAR_POST",
      "freeKickEndZone": null,
      "freeKickType": null,
      "goalKickEndZone": null,
      "goalKickType": null,
      "throwInEndZone": null,
      "throwInType": null,
      "secondDeliveryEndZone": null,
      "secondDeliveryType": null,
      "mainEventPlayerId": 5632,
      "mainEventOutcome": "UNSUCCESSFUL",
      "passReceiverId": 89548,
      "ballTrajectory": "INSWINGING",
      "firstTouchPlayerId": null,
      "firstTouchWon": false,
      "indirectHeader": "NONE",
      "secondTouchPlayerId": 103502,
      "secondTouchWon": true,
      "secondTouchEndZone": "NEAR_POST_WIDE",
      "aggregates": {
        "SHOT_XG": 0.0569, "PACKING_XG": 0.0399, "POSTSHOT_XG": 0.2189,
        "SHOT_AT_GOAL_NUMBER": 1.0, "GOALS": 0.0, "PXT_POSITIVE": 0.0485,
        "BYPASSED_OPPONENTS": 0.0, "BYPASSED_DEFENDERS": 0.0
      }
    }
  ]
}
```

**Why this matters, concretely:**

- `cornerType` is a real IMPECT label (`CORNER_NEAR_POST`, presumably
  `CORNER_CENTRAL`/`CORNER_FAR_POST`/`CORNER_OPEN_PLAY`/etc. — not
  independently confirmed for every value yet, see §6) in the **same raw
  vocabulary** `pre_match_report/config.py`'s `IMPECT_CORNER_TYPE_MAP` in the
  report repo already expects. No remapping work needed downstream, just a
  real source for the column instead of a guess.
- `ballTrajectory` (`INSWINGING`/presumably `OUTSWINGING` — one confirmed
  live, see raw dump below) is swing direction, which every doc in the
  report repo currently says doesn't exist in the feed.
- `firstTouchPlayerId` / `firstTouchWon` / `secondTouchPlayerId` /
  `secondTouchWon` / `secondTouchEndZone` are given directly, replacing the
  report repo's fragile "scan forward for the next event with a non-null
  player name" derivation — which is exactly what broke for Cardiff City and
  AFC Wrexham this season (see §7, related but separate bug already fixed in
  the report repo).
- `mainEventPlayerId` is the corner/FK taker directly.
- One `FREE_KICK` phase was also captured live in the same pull, confirming
  the shape generalises: `freeKickType: "FREE_KICK_CROSS_CENTRAL"`,
  `freeKickEndZone: "CENTRAL_WIDE"`, `ballTrajectory: "OUTSWINGING"`.

## 3. Confirmed NOT currently ingested

Grepped the whole of `python/extract/impect/` and `python/orchestrator.py`
for `set-pieces` / `setPieces` / `SET_PIECES` — no hits. The existing
extractor surface (`load_match_events.py`, `load_match_info.py`,
`load_iteration_player_kpis.py`, etc.) covers events, match metadata, KPIs,
squads/players/coaches/stadiums — nothing calls this endpoint. There is no
`CAFC_DB.IMPECT_RAW.SET_PIECES` table.

`impect_api.py` itself has no wrapper function for this endpoint either
(every other endpoint has one, e.g. `get_match_events`, `get_match_info`) —
step 1 below adds it following that same pattern.

## 4. Proposed implementation

Follows the existing codebase conventions closely — this is deliberately
*not* a new pattern, just filling a gap in an existing one.

### 4.1 `impect_api.py` — new wrapper function

Add alongside `get_match_info` (~line 391):

```python
def get_match_set_pieces(match_id: int, params: Optional[Dict] = None) -> Dict[str, Any]:
    """Get set-piece sub-phase data (corner/FK/throw-in type, swing direction,
    first/second touch) for a specific match."""
    return make_request(f"/v5/customerapi/matches/{match_id}/set-pieces", params)
```

### 4.2 New Snowflake table — `CAFC_DB.IMPECT_RAW.SET_PIECES`

Model on `snowflake/ddl/20260827_impect_match_info.sql`'s
match-metadata pattern (one JSON payload per match, not per-event like
`EVENTS`) rather than the flattened `EVENTS` table — the phase count per
match is small (83 in the sample) and the nested `setPieceSubPhase` array is
naturally a VARIANT column, avoided-normalisation the same way
`HOME_PLAYERS`/`AWAY_PLAYERS` are on `MATCH_INFO`.

Two reasonable shapes — **recommend (b)**, decide before implementing:

**(a) One row per match** (mirrors `MATCH_INFO`): `RAW_SET_PIECES` VARIANT
holds the full `data` array; consumers flatten in dbt/pandas. Simplest
loader, but every downstream query has to unnest.

**(b) One row per phase, flattened sub-phase fields inline** (mirrors
`EVENTS`'s per-row philosophy, and matches what `pre_match_report`'s
consumer code already expects as a column shape): since one phase can carry
multiple sub-phases (recycled corners), either flatten to one row per
sub-phase (join key `SET_PIECE_ID` + `SUB_PHASE_INDEX`) or keep
`SUB_PHASES` as a VARIANT array on the phase row. Given
`pre_match_report/cafcdb_source.py`'s consumer shape already expects one row
per sub-phase-touching-event (see §5), **one row per sub-phase** is the
lower-friction target:

```sql
CREATE TABLE IF NOT EXISTS CAFC_DB.IMPECT_RAW.SET_PIECES (
  MATCH_ID                NUMBER(38,0) NOT NULL,
  ITERATION_ID             NUMBER(38,0),
  SET_PIECE_ID             NUMBER(38,0) NOT NULL,       -- phase id ("id" above)
  PHASE_INDEX               NUMBER(38,0),
  SQUAD_ID                  NUMBER(38,0),                -- attacking squad
  SET_PIECE_CATEGORY        VARCHAR(30),                 -- CORNER_LEFT/RIGHT, FREE_KICK, THROW_IN
  ADJ_SET_PIECE_CATEGORY    VARCHAR(30),
  SET_PIECE_EXECUTION_TYPE  VARCHAR(30),
  START_TIME_IN_SEC         FLOAT,
  END_TIME_IN_SEC           FLOAT,
  SUB_PHASE_ID              NUMBER(38,0) NOT NULL,
  SUB_PHASE_INDEX            NUMBER(38,0),
  START_ZONE                 VARCHAR(40),
  CORNER_END_ZONE             VARCHAR(40),
  CORNER_TYPE                 VARCHAR(40),
  FREE_KICK_END_ZONE          VARCHAR(40),
  FREE_KICK_TYPE               VARCHAR(40),
  THROW_IN_END_ZONE            VARCHAR(40),
  THROW_IN_TYPE                 VARCHAR(40),
  BALL_TRAJECTORY                VARCHAR(20),            -- INSWINGING / OUTSWINGING
  MAIN_EVENT_PLAYER_ID            NUMBER(38,0),
  MAIN_EVENT_OUTCOME               VARCHAR(20),
  PASS_RECEIVER_ID                  NUMBER(38,0),
  FIRST_TOUCH_PLAYER_ID              NUMBER(38,0),
  FIRST_TOUCH_WON                     BOOLEAN,
  INDIRECT_HEADER                      VARCHAR(10),
  SECOND_TOUCH_PLAYER_ID                NUMBER(38,0),
  SECOND_TOUCH_WON                       BOOLEAN,
  SECOND_TOUCH_END_ZONE                   VARCHAR(40),
  AGGREGATES                               VARIANT,       -- SHOT_XG/PACKING_XG/etc.
  RAW_PHASE                                 VARIANT,       -- full phase JSON, for anything missed above
  SOURCE_FORMAT                              VARCHAR(30) NOT NULL DEFAULT 'SET_PIECES_API',
  LOADED_AT                                   TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
  INGESTION_RUN_ID                             NUMBER(38,0)
)
CLUSTER BY (ITERATION_ID, MATCH_ID)
COMMENT = 'IMPECT set-piece sub-phase classification (corner/FK/throw-in type, swing direction, first/second touch) from GET /matches/{id}/set-pieces -- real labels, not the geometry-derived approximation used before this table existed.';
```

Confirm the full field list against a couple more live matches before
finalising DDL (§6) — the sample above is from one match's `CORNER` and
`FREE_KICK` phases; `THROW_IN`/`GOAL_KICK` phase shapes weren't inspected.

### 4.3 New loader — `load_set_pieces.py`

Copy `load_match_info.py`'s structure almost directly — same per-match
fetch-and-upsert, same `--backfill-set-pieces` / `--limit` /
`--pause-seconds` / `--lane` / `--lanes` / `--dry-run` CLI, same
"matches present in `EVENTS` but missing from this table" backfill query
(swap the `LEFT JOIN {TABLE}` target). Flatten `data[].setPieceSubPhase[]`
into one row per sub-phase inside `payload_row`, `MERGE`-upsert keyed on
`(SET_PIECE_ID, SUB_PHASE_ID)` instead of `MATCH_ID` alone (a match has many
phases).

### 4.4 Orchestrator + backfill

Add `"load_set_pieces.py"` to the `scripts` list in
`orchestrator.py::step_extract_impect()`, after `load_match_events.py`-style
steps that need event data present first (the backfill query joins against
`EVENTS`). Historical backfill: same `run_championship_backfill.sh` /
`run_queue_backfill_lanes.sh` pattern already used for events — check those
scripts for the current lane count and pacing before reusing.

## 5. Downstream: `pre-match-set-piece-report` repo changes (not in scope here)

Once `CAFC_DB.IMPECT_RAW.SET_PIECES` exists and is backfilled for
Championship 26/27 at minimum, `pre_match_report/cafcdb_source.py` in the
**`pre-match-set-piece-report`** repo should switch from deriving
`setPieceSubPhaseCornerType`/`setPieceSubPhaseFreeKickType`/
`setPieceSubPhaseFirstTouchWon`/`setPieceSubPhaseFirstTouchPlayerName` via
`_derive_set_piece_fields`/`_corner_type`/`_post_zone`/`_fk_type` to a join
against this new table (`SET_PIECE_ID` there = `setPieceId`/`subPhaseId`
already used as the join key throughout `metrics.py`). That removes the
geometry-heuristic caveats currently documented in `config.py` and adds
`ballTrajectory` as new, previously-unavailable data (the dossier could add
an in/out-swinging split to the corners/FK pages). **This is a separate PR
in a separate repo** — flagged here so whoever does the Snowflake side knows
there's a real, waiting consumer, not just a hypothetical one.

Player-name resolution for `mainEventPlayerId`/`firstTouchPlayerId` will
still go through `CAFC_DB.IMPECT_RAW.PLAYERS`, which has a **separate,
already-identified bug**: newly-promoted/relegated squads (Cardiff City, AFC
Wrexham this season) have no `PLAYERS` rows under the current iteration id,
only past ones — already fixed on the consumer side in
`pre-match-set-piece-report/pre_match_report/cafcdb_source.py`
(`_backfill_missing_player_names`, 2026-09-15) via an iteration-agnostic
fallback lookup. Worth considering whether that fallback belongs upstream in
`cafc-data-platform`'s `PLAYERS` loader/dbt model instead, so every consumer
gets it for free rather than each report repo re-deriving it.

## 6. Open questions before implementing

1. Full enum values for `cornerType`, `freeKickType`, `throwInType`,
   `goalKickType`, `ballTrajectory`, `mainEventOutcome`, `indirectHeader` —
   only a handful of values were observed in one match's 83 phases. Pull a
   larger sample (a few matches across different squads) and diff against
   `pre_match_report/config.py`'s `IMPECT_CORNER_TYPE_MAP` /
   `IMPECT_FK_TYPE_MAP` keys to confirm 1:1 coverage before cutting the
   report repo over.
2. Rate limits / pagination — not tested at volume. `impect_api.py`'s
   `make_request` already has retry/backoff (used by every other endpoint);
   confirm this endpoint behaves the same under the existing
   `run_championship_backfill.sh` pacing before a full historical run.
3. Historical coverage boundary — does `/set-pieces` go back as far as
   `/events` (season 21/22 per `load_match_events.py`'s docstring), or is it
   newer/product-tier-gated? Check a known-old match id before promising
   full backfill.
4. Table shape (a) vs (b) in §4.2 — recommend deciding based on how the
   *first* real consumer (`pre-match-set-piece-report`) wants to query it,
   since that repo's `metrics.py` already assumes one-row-per-sub-phase-event
   semantics.

## 7. Where this came from

Full investigation trail (report repo, not here):
`pre-match-set-piece-report`, session ending 2026-09-15 — regenerating a
Cardiff City pre-match dossier surfaced (a) cup-competition contamination of
the "last 10 games" sample (fixed, unrelated to this doc), (b) the
missing-`PLAYERS`-row-for-new-iteration bug (fixed, mentioned in §5), and
(c) the corner-classification mismatch that led to this endpoint being
found. No corresponding memory/doc exists yet in that repo beyond its own
code comments — this file is the first place all three findings are written
down together.
