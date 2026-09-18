# Using the raw event/physical data — a guide for building new tools on it

Written for a fresh Claude session (or human) starting a new project — a
report generator, an analysis notebook, a model — on top of the raw data
landed by this platform. Assumes no prior context. Everything here was
verified live against the real Snowflake account and the real provider APIs
as of 2026-07-29, not inferred from docs alone.

## The short version

Three raw data sources exist in `CAFC_DB`, at three different levels of
"ready to query":

| Source | Table(s) | Ready to query with plain SQL? | Real depth |
|---|---|---|---|
| Impect match events | `CAFC_DB.IMPECT_RAW.EVENTS` | **Yes** — fully typed columns | 2021/22 season onward, most competitions (see below) |
| DVMS (Opta + Second Spectrum) | `CAFC_DB.DVMS_RAW.FIXTURES` / `.ASSETS` / `.POSITIONAL_STAGE` | **No** — raw XML/CSV/JSONL, needs parsing | 2025/26 season only (Championship) |
| SkillCorner physical data | `CAFC_DB.SKILLCORNER_RAW.PHYSICAL_SUMMARY` | **Yes** — flat columns | 2024/25 + 2025/26 (Championship, League One), plus partial European coverage |

There is also a separate, older, ungoverned database — `CAFC_TEST_ANALYSIS`
— that most of the club's *existing* report tooling (`board-post-match-report`,
`set-piece-report`, etc.) actually reads from today. It is not part of this
platform and is not documented here except where it matters for context (see
"A note on `CAFC_TEST_ANALYSIS`" at the end).

## Connecting to Snowflake

Account/user/warehouse are the same regardless of which schema you're
reading. This platform's own connection helper is `python/_snowflake.py`
(`from python import _snowflake; conn = _snowflake.get_connection()`), which
reads credentials from `python/extract/impect/.env` (gitignored). Role is
`DEV_ROLE` today (`DATA_PLATFORM_ROLE` is named throughout docs/DDL but not
actually provisioned in the live account).

If you're building a standalone project (not inside this repo), you need
your own `.env` with `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`,
`SNOWFLAKE_PRIVATE_KEY_PATH`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE` — RSA
key-pair auth, not password. `board-post-match-report` (see below) vendors
its own connector (`src/db/snowflake_connection.py`) rather than depending on
this repo; that's a reasonable pattern to copy.

---

## 1. Impect match events (`IMPECT_RAW.EVENTS`)

The easiest of the three — one row per on-ball event, already flattened into
typed Snowflake columns, no parsing needed.

### Grain and identity

One row per `(MATCH_ID, EVENT_ID)`. `MATCH_ID` joins to `IMPECT_RAW.MATCHES`;
`PLAYER_ID` and `SQUAD_ID` join to `IMPECT_RAW.PLAYERS`/`SQUADS`. These are
Impect's own numeric IDs — not the platform's canonical `CAFC_PLAYER_ID`. If
you ever need to join this to the canonical layer (`CORE`), go through
`CORE.core_player_id_resolutions` on `(source_system='IMPECT',
source_player_id=PLAYER_ID)` — never assume Impect's `PLAYER_ID` is stable
outside this table.

### Columns

Common scalar fields are real columns: `EVENT_INDEX`, `SEQUENCE_INDEX`,
`PERIOD_ID`, `GAME_TIME`, `GAME_TIME_IN_SEC`, `SQUAD_ID`,
`CURRENT_ATTACKING_SQUAD_ID`, `PLAYER_ID`, `PLAYER_POSITION`,
`PLAYER_POSITION_SIDE`, `ACTION_TYPE`, `ACTION`, `PHASE`, `BODY_PART`,
`BODY_PART_EXTENDED`, `DURATION`, `OPPONENTS`, `PRESSURE`, `DISTANCE_TO_GOAL`,
`DISTANCE_TO_OPPONENT`, `RESULT`, `PRESSING_PLAYER_ID`, `FOULED_PLAYER_ID`,
`INFERRED_SET_PIECE`.

Nested/conditional detail is kept as `VARIANT` columns, one per
event-type-specific group (a `shot` object only exists on shot events, a
`duel` object only on duels, etc.) — deliberately not flattened, since
flattening ~10 mostly-null sub-objects into one wide row loses the
"which fields are actually valid for this event" signal:

- `START_DETAIL` / `END_DETAIL`: `{coordinates: {x,y}, adjCoordinates: {x,y}, packingZone, pitchPosition, lane}`
- `SHOT_DETAIL`: `{distance, angle, targetPoint: {y,z}, gk: {...}, woodwork}`
- `PASS_DETAIL`: `{distance, angle, receiver: {playerId, type}}`
- `DUEL_DETAIL`, `DRIBBLE_DETAIL`, `SET_PIECE_DETAIL`: event-specific, often null
- `PXT_DETAIL`: `{team, opponent}` — Impect's possession-value-added metric for this event
- `FORMATION_DETAIL`: `{team, opponent}` formation strings at that moment
- `OPPONENT_DETAIL`: nearest-opponent coordinates
- `EVENT_KPIS`: **event-level xG lives here, not on the plain event fields** —
  an array of `{position, playerId, <kpiName>: value, ...}` objects, one per
  player/position combination attributed to that event (typically ~10 per
  event: the primary player plus every other on-pitch player's attribution,
  e.g. every outfield player gets a `DEF_PXT_SHOT` value for one shot).
  Sourced from a genuinely separate endpoint (`/matches/{id}/event-kpis`,
  resolved against `/kpis/event` for the 103 KPI names) — confirmed live
  that `SHOT_XG`/`PACKING_XG`/`POSTSHOT_XG` and the full `PXT_*` family do
  not exist anywhere on the base `/events` payload at all. Filter the array
  by `playerId` to get one player's view of an event.
- `RAW_EVENT`: the full verbatim event JSON, for anything not worth a dedicated column

Query nested fields with Snowflake's `:field::type` syntax, e.g.:

```sql
select
    MATCH_ID, PLAYER_ID, ACTION,
    SHOT_DETAIL:distance::float as shot_distance,
    PXT_DETAIL:team::float as pxt_team
from CAFC_DB.IMPECT_RAW.EVENTS
where ACTION_TYPE = 'SHOT'
  and MATCH_ID = 206530
```

Event-level xG needs a `LATERAL FLATTEN` since `EVENT_KPIS` is an array, e.g.
to get the shooter's own `SHOT_XG` for every shot in a match:

```sql
select e.EVENT_ID, e.PLAYER_ID, f.value:"SHOT_XG"::float as shot_xg
from CAFC_DB.IMPECT_RAW.EVENTS e, lateral flatten(input => e.EVENT_KPIS) f
where e.ACTION_TYPE = 'SHOT'
  and e.MATCH_ID = 206530
  and f.value:playerId::int = e.PLAYER_ID
```

### Real historical depth — do not assume 10 years

**Event-level data only exists from Impect's `dataVersion` V2 onward** —
confirmed empirically by calling the live API across every season for every
competition Impect tracks (203 competitions checked, 2026-07-24):

- The English Championship, Premier League, and essentially every other
  major domestic league: **2021/22 onward**. Earlier seasons return the
  API's own explicit error, `{"message": "Match does not have packing plus
  data"}` — Impect itself never tracked events that far back, this is not
  a gap in what's been pulled.
- One confirmed exception: the **FIFA World Cup** goes back to **2014**.
  Every other international tournament (Euros, Copa América, etc.) only
  goes as far back as Impect started tracking that specific competition at
  all (mostly 2022-2025) — no other tournament shares the World Cup's depth.
- `IMPECT_RAW.ITERATIONS` lists competition-seasons back to 2015/16 —
  that's just Impect's metadata catalogue, not evidence that event data
  exists for them. Check the actual `dataVersion` column on
  `IMPECT_RAW.ITERATIONS` (or just try the query) before assuming depth for
  a new competition.

### What's actually loaded right now

As of the last backfill run, coverage is EFL Championship 2025/26
(`ITERATION_ID = 1410`) — check current state with:

```sql
select ITERATION_ID, count(distinct MATCH_ID) as matches, count(*) as events
from CAFC_DB.IMPECT_RAW.EVENTS
group by 1
```

### Gotcha: Impect can silently re-process a match, invalidating its event IDs

Confirmed live (2026-07-30): one already-loaded match (206877) had every one
of its event IDs regenerated by Impect at some point after we first loaded
it — the plain `/events` endpoint now returns a completely different ID
range for the same match. The loader's idempotency check only looks at
whether a `MATCH_ID` is already present, so this went undetected until an
unrelated KPI-attach step found zero matching event IDs for that match. If
you're re-running any incremental sync against already-loaded matches,
don't assume a match's event IDs are permanently stable — there is currently
no automated detection for this; the fix when it happens is to delete that
match's rows from `IMPECT_RAW.EVENTS` and reload it fresh with `--force`.

### Extending it

`python/extract/impect/load_match_events.py` (in this repo) —
`python load_match_events.py --iteration-id <id>` pulls every match in one
Impect iteration; `--match-ids 123,456` pulls specific matches. Idempotent
(skips matches already loaded). A GitHub Actions workflow
(`.github/workflows/impect-events-backfill.yml`) runs this on GitHub's
infrastructure rather than needing a laptop kept on — trigger it from the
Actions tab or `gh workflow run impect-events-backfill.yml -f iteration_id=<id>`.

---

## 2. DVMS (Opta + Second Spectrum) — `DVMS_RAW`

This is the one that needs real parsing work — nothing here is a clean
table. **Don't build a parser from scratch**: a complete, tested, working
reference implementation already exists at
`/Users/hashim.umarji/Projects/board-post-match-report/src/dvms/` (a vendored
package also copied into sibling repos `charlton-post-match-analyst`,
`set-piece-report`, `pre-match-set-piece-report`, `dvms-sample-pack` — see its
`VENDOR.md`). It has unit tests (`tests/dvms/`) run against real sample files.
Copy it rather than re-deriving the XML/CSV parsing from scratch.

### The tables

- **`DVMS_RAW.FIXTURES`** — one row per fixture, includes `FIXTURE_ID` (the
  join key to `ASSETS`), `OPTA_MATCH_ID`, team names, scores, kickoff date.
- **`DVMS_RAW.ASSETS`** — one row per (fixture, asset). `ASSET_SUBTYPE` is
  DVMS's own enum code (below). Small assets have their content inline in
  `RAW_PAYLOAD` (text). The one big asset (positional tracking) has
  `RAW_PAYLOAD` null and instead sets `STAGED_AT` once its file is in the
  stage.
- **`DVMS_RAW.POSITIONAL_STAGE`** — an internal Snowflake *stage*, not a
  table. Files sit here gzip-compressed, one per fixture, at the path in
  `ASSETS.ASSET_KEY`. You `GET` a file out to local disk to read it — you
  cannot `SELECT` it directly.

### Asset subtype codes (what's actually in `RAW_PAYLOAD`)

| Subtype | Contents | Format | Size | Where |
|---|---|---|---|---|
| 20 | Opta F24 — event data (every pass/shot/tackle) | XML | ~1MB | `ASSETS.RAW_PAYLOAD` |
| 21 | Opta F7 — team sheet / lineups | XML | ~20KB | `ASSETS.RAW_PAYLOAD` |
| 38 | Second Spectrum tracking (frame-by-frame positions) | JSONL | ~420-470MB | `POSITIONAL_STAGE` (staged, not inline) |
| 39 | Same tracking data, XML format | XML | same size | not staged — subtype 38 covers it, no reason to store both |
| 40 | Second Spectrum metadata (pitch dims, player/team IDs) | JSON | <10KB | `ASSETS.RAW_PAYLOAD` |
| 41 | Same metadata, XML format | XML | <10KB | `ASSETS.RAW_PAYLOAD` |
| 42 | Second Spectrum physical **splits** (minute-by-minute) | CSV | <40KB | `ASSETS.RAW_PAYLOAD` |
| 43 | Second Spectrum physical **summary** (match totals) | CSV | <40KB | `ASSETS.RAW_PAYLOAD` |

Fetching an inline asset:

```sql
select RAW_PAYLOAD
from CAFC_DB.DVMS_RAW.ASSETS
where FIXTURE_ID = '<fixture_id>' and ASSET_SUBTYPE = 20
order by LOADED_AT desc limit 1
```

Getting the positional file requires a `GET` (Python, via a cursor):

```python
cur.execute(f"GET @CAFC_DB.DVMS_RAW.POSITIONAL_STAGE/{asset_key} file://./local_dir/")
```

`asset_key` comes from `ASSETS.ASSET_KEY` for the subtype-38 row.

### Parsing each format — what the reference implementation does

- **F24 (events, subtype 20)**: `xml.etree.ElementTree`, one `<Event>` per
  row, qualifiers (`<Q qualifier_id=... value=.../>`) collected into a dict
  per event — Opta's taxonomy has ~80 event type codes and a much longer
  qualifier list, so keep the *full* qualifier set per row rather than
  picking a few upfront (different downstream uses need different
  qualifiers). Coordinates are 0-100 pitch-scale, independent of real pitch
  dimensions.
- **F7 (team sheet, subtype 21)**: same XML approach, gives you player
  names/IDs/shirt numbers/positions per team — this is what resolves
  Opta's numeric player IDs to names.
- **Second Spectrum metadata (subtype 40)**: JSON, gives real pitch
  dimensions (needed to convert Opta's 0-100 coordinates into the same
  metre-based frame the tracking data uses) and team/player ID mappings.
- **Second Spectrum physical (subtype 42/43)**: **not** a plain CSV — the
  summary file has a title/preamble before the real header row, and repeats
  the header for a second player block (home team, then away team), so
  `pd.read_csv` misreads it if used naively. The reference parser
  (`src/dvms/parsers/ss_physical.py`) handles this with a custom multi-section
  reader. Gives per-player distance, HSR, sprints, top/average speed, and the
  TIP/OTIP/BOP (team-in-possession / opponent-in-possession / ball-out-of-play)
  splits per metric — this is the authoritative provider-computed physical
  data, don't try to recompute it from tracking.
- **Tracking (subtype 38)**: ~1.5-2M JSON frames per match — never parse this
  twice. Stream it once, downsample (the reference implementation keeps every
  5th frame, i.e. 5Hz from a 25fps feed), and cache the result as parquet.
  Coordinates are pitch-centred metres (`(0,0)` = centre spot), a different
  frame from Opta's 0-100 — `src/dvms/coords.py` has the verified conversion
  (including a load-bearing, empirically-verified sign convention for the
  y-axis — get this wrong and every event lands on the wrong side of the
  pitch).

### Known gotchas (verified, not assumed)

- **DVMS's own `READY` flag on an asset is unreliable** — it can read
  `false` even when the asset downloads successfully. Availability is
  determined empirically (attempt the download, treat failure as
  unavailable), not by trusting the flag.
- **Rate limits are real but unconfirmed** — a 2026-07-17 test run with no
  pacing at all triggered DVMS's anti-abuse protection badly enough to
  temporarily block the login endpoint itself. The extractor in this repo
  paces every download (`DOWNLOAD_DELAY_SECONDS`); don't remove that pacing
  when building something new against this API.
- **No history before 2025/26** — confirmed empirically across every season
  back to 2020 for Charlton's account; this genuinely is the entire
  available history from this provider today, not a partial backfill.

### What's actually loaded right now

Fixtures/small-asset content (subtypes 20/21/40-43) are essentially fully
backfilled for the 2025/26 Championship season (557 fixtures, ~97-98% asset
coverage — a handful of fixtures have persistent gaps on DVMS's side, not
worth chasing). Positional tracking (subtype 38) is partially staged — check
current coverage:

```sql
select count(*) as staged_matches
from CAFC_DB.DVMS_RAW.ASSETS
where ASSET_SUBTYPE = 38 and STAGED_AT is not null
```

### Extending it

In this repo: `python -m python.extract.dvms.load_dvms_fixtures` (fixtures +
small assets, weekly-scheduled via `.github/workflows/dvms-weekly.yml`, also
manually triggerable) and `python -m python.extract.dvms.load_dvms_positional`
(positional tracking, manual — `.github/workflows/dvms-positional-backfill.yml`).
Both idempotent.

For actually *reading* the data in a new project: use
`board-post-match-report/src/dvms/loaders/` and `.../parsers/` directly
(copy the package, per its own `VENDOR.md` convention) rather than
re-implementing any of this — it already handles every gotcha above, is
tested against real sample files, and has a CLI
(`python -m src.dvms.cli list-fixtures` / `prefetch` / `preprocess`) for
exploring what's available.

---

## 3. SkillCorner physical data (`SKILLCORNER_RAW.PHYSICAL_SUMMARY`)

Flat, queryable, no parsing needed — one row per (match, player).

### Auth (undocumented publicly, reverse-engineered)

Not a header — a `token` query parameter on every request
(`?token=<api_key>`). Every header-based scheme (`Authorization: Token/
Bearer/Api-Key`, `X-Api-Key`) fails.

### Coverage — check before assuming a competition/season is in scope

The account's real match access is **not** limited to Charlton's own
competition. Confirmed live (2026-07-27): 7 (competition, season) pairs, 3,005
matches total — English Championship (24/25 + 25/26), League One (24/25),
League Two (24/25), plus partial Eredivisie/Allsvenskan/Scottish Premiership
coverage. This tracks Charlton's actual competition history (League One last
season, Championship this season) rather than being an arbitrary bundle.
Every earlier season (back to 2019/20, which *is* listed in SkillCorner's own
catalogue) returns zero matches for this account — listed in the catalogue
doesn't mean accessible.

```sql
select count(*), count(distinct MATCH_ID)
from CAFC_DB.SKILLCORNER_RAW.PHYSICAL_SUMMARY
```

To discover what's currently accessible rather than trust a hardcoded list,
use `skillcorner_api.get_available_competition_editions()` in
`python/extract/skillcorner/` — it pages through every match the account can
see and reduces to distinct competition/edition pairs.

### Gotcha: matches with no physical data return a plain 404

Not every match in a covered competition/season necessarily has physical
data — some return HTTP 404. `load_physical_summary.py` handles this (skip
and continue); if you're querying the API directly, don't assume every match
ID in a season succeeds.

### Extending it

`python load_physical_summary.py --all-available` (discovers and backfills
every accessible competition/edition) or `--competition-edition <id>` /
`--match-ids <ids>` for a narrower pull. GitHub Actions workflow:
`.github/workflows/skillcorner-backfill.yml`.

---

## A note on `CAFC_TEST_ANALYSIS`

Most of the club's *existing* report tooling (`board-post-match-report`,
`set-piece-report`, `pre-match-set-piece-report`) reads from
`CAFC_TEST_ANALYSIS.PUBLIC.IMPECT_EVENTS_STAGING`, **not**
`CAFC_DB.IMPECT_RAW.EVENTS`. This is a separate, older, ungoverned database —
built by ad-hoc scripts outside this platform and outside dbt entirely — and
it is genuinely a different (larger, older) dataset than what's in `CAFC_DB`
today: it covers the whole 2025/26 Championship season already, whereas
`IMPECT_RAW.EVENTS` in `CAFC_DB` is still mid-backfill.

If you're building something new, **prefer `CAFC_DB.IMPECT_RAW.EVENTS`** —
same underlying provider, same event shape (confirmed: the two tables' shapes
match closely enough that `IMPECT_EVENTS_STAGING`'s column list was one of the
sources used to design `IMPECT_RAW.EVENTS`'s schema), but it's the governed,
platform-owned copy that this session's backfill work is actively completing.
`CAFC_TEST_ANALYSIS` is flagged in the platform's own architecture review as
something to eventually fold into the governed platform or retire — don't
build new long-term dependencies on it if you can build on `CAFC_DB` instead.
One caveat: `IMPECT_RAW.EVENTS` doesn't yet have the same season-long
coverage `CAFC_TEST_ANALYSIS` already has (backfill in progress as of
2026-07-29) — check row counts for your target match before assuming it's
there.

See `board-post-match-report/DATA_MODEL.md` for a very thorough worked
example of building a real single-match report off `IMPECT_EVENTS_STAGING`
— match-clock parsing (`gameTimeInSec` is NOT a usable clock, `gameTime` is),
the pxT double-counting trap (`PXT_ATTACK` not the per-player `PXT_PASS`/
`PXT_REC` columns), and several other traps that apply equally whichever of
the two Impect event tables you use, since they're the same underlying data.
