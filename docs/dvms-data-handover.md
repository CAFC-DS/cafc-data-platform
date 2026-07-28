# DVMS data handover — Opta event data + Second Spectrum tracking data

**Purpose of this document**: you (a Claude instance working on match reports /
visuals) have been handed this to understand what football data is available,
where it lives, and how to pull it out. Everything described here was
verified against real, live data on 2026-07-17 — sample values quoted below
are actual values from a real match (Swansea City 3-1 Charlton Athletic,
2026-05-02), not illustrative placeholders.

## 1. What this is

Data pulled from the Premier League's DVMS portal (a Hudl-built system EFL
Championship clubs use), covering two providers per match:
- **Opta** — event data (every pass, shot, tackle, etc.) and team
  sheets/lineups. Industry-standard football event data.
- **Second Spectrum** (shown as "GeniusIQ" in the DVMS web UI) — optical
  tracking data: x/y/z position of all 22 players + the ball, 25 times per
  second, for the full duration of the match, plus derived physical/fitness
  stats.

Video is also catalogued but never downloaded (files are multiple GB each —
not useful for this kind of analysis and not worth the storage).

## 2. Where it lives

Everything is in Snowflake, database `CAFC_DB`, schema `DVMS_RAW`:

| Object | What it is |
|---|---|
| `CAFC_DB.DVMS_RAW.FIXTURES` | One row per match: teams, date, score, Opta IDs |
| `CAFC_DB.DVMS_RAW.ASSETS` | One row per file per match: what it is, and (for small files) the actual content inline |
| `CAFC_DB.DVMS_RAW.POSITIONAL_STAGE` | A Snowflake internal stage holding the large positional tracking files (too big to fit in a table column) |

Connection: same Snowflake account the rest of `cafc-data-platform` uses.
If you have access to that repo, connection details/credentials are in
`python/_snowflake.py` + `python/extract/impect/.env` (gitignored — ask the
user for credentials if you don't have repo access). Role `DEV_ROLE`,
warehouse `DEVELOPMENT_WH`.

## 3. What's actually loaded right now

**Only Charlton Athletic's last 10 played games** (2026-03-11 to
2026-05-02), full data for all of them. The rest of the Championship season
is *not* loaded yet. Check current coverage before assuming a team/match you
need is there:

```sql
SELECT FIXTURE_ID, MATCH_DATE, HOME_TEAM_NAME, AWAY_TEAM_NAME, HOME_SCORE, AWAY_SCORE, OPTA_MATCH_ID
FROM CAFC_DB.DVMS_RAW.FIXTURES
ORDER BY MATCH_DATE DESC;
```

If the match(es) you need aren't there, see §8 "getting more data" — don't
try to pull from DVMS yourself; ask the user to run the extractor, since it
has hard-won rate-limiting protections that must not be bypassed (see §8).

## 4. `DVMS_RAW.FIXTURES` — match list

Key columns: `FIXTURE_ID` (join key to ASSETS), `OPTA_MATCH_ID` (e.g.
`"g2566913"` — the id embedded in every Opta/Second Spectrum file for that
match), `OPTA_HOME_TEAM_ID`/`OPTA_AWAY_TEAM_ID` (e.g. `"t80"`, `"t33"`),
`HOME_TEAM_NAME`/`AWAY_TEAM_NAME`, `MATCH_DATE`, `HOME_SCORE`/`AWAY_SCORE`,
`ROUND`. `RAW_PAYLOAD` has the full original JSON if you need something not
broken out into a column.

## 5. `DVMS_RAW.ASSETS` — the actual files

One row per (fixture, file). Get everything for a match:

```sql
SELECT ASSET_ID, ASSET_TYPE, ASSET_SUBTYPE, ASSET_KEY, RAW_PAYLOAD, STAGED_AT
FROM CAFC_DB.DVMS_RAW.ASSETS
WHERE FIXTURE_ID = '6867e5bfdd404da9156b2f3e'; -- Swansea 3-1 Charlton example
```

`ASSET_SUBTYPE` tells you what the file is. Only these subtypes are ever
actually populated (everything else — video, panoramic — is catalogued as a
metadata-only row with no content, by design):

| ASSET_SUBTYPE | What | Format | Size | Where the content is |
|---|---|---|---|---|
| 21 | Opta F7 — team sheet/lineup | XML | ~18KB | `ASSETS.RAW_PAYLOAD` (inline text) |
| 20 | Opta F24 — event data | XML | ~900KB-1MB | `ASSETS.RAW_PAYLOAD` (inline text) |
| 40 | Second Spectrum metadata | JSON | ~10KB | `ASSETS.RAW_PAYLOAD` (inline text) |
| 41 | Second Spectrum metadata | XML | <1KB | `ASSETS.RAW_PAYLOAD` (inline text) |
| 42 | Second Spectrum physical splits (minute-by-minute, team level) | CSV | ~40KB | `ASSETS.RAW_PAYLOAD` (inline text) |
| 43 | Second Spectrum physical summary (per-player) | CSV | ~5KB | `ASSETS.RAW_PAYLOAD` (inline text) |
| 38 | **Second Spectrum positional tracking** (frame-by-frame x/y/z) | JSONL | ~450-500MB raw / ~28MB compressed | `POSITIONAL_STAGE`, NOT inline — see §7 |

`RAW_PAYLOAD` is text either way (even for the JSON ones) — `json.loads()`
or your XML parser of choice, don't expect a Snowflake VARIANT type.

## 6. File format reference (verified structure, real examples)

### Opta F7 (ASSET_SUBTYPE=21) — lineups, formation, goals

Two useful sections:

```xml
<TeamData AverageAge="26.2" Formation="4231" Score="3" Side="Home" TeamRef="t80">
  <Goal EventID="2931910955" Min="73" PlayerRef="p432735" Sec="40" Type="Goal" uID="g80-1">
    <Assist PlayerRef="p434128">p434128</Assist>
  </Goal>
  <PlayerLineUp>
    <MatchPlayer Formation_Place="1" PlayerRef="p198965" Position="Goalkeeper" ShirtNumber="1" Status="Start" />
    <MatchPlayer Formation_Place="0" PlayerRef="p626464" Position="Substitute" ShirtNumber="18" Status="Sub" SubPosition="Forward" />
    ...
  </PlayerLineUp>
</TeamData>
```

Player name lookup (`PlayerRef` → name) is in a separate `<Team>` block:

```xml
<Team uID="t80">
  <Name>Swansea City</Name>
  <Player Position="Goalkeeper" uID="p198965">
    <PersonName><First>Andy</First><Last>Fisher</Last></PersonName>
  </Player>
  ...
</Team>
```

### Opta F24 (ASSET_SUBTYPE=20) — event data, THE file for shot/pass maps

```xml
<Games timestamp="...">
  <Game id="2566913" home_team_id="80" away_team_id="33" home_score="3" away_score="1" ...>
    <Event id="2931816641" event_id="3" type_id="1" period_id="1" min="0" sec="0"
           player_id="215069" team_id="33" outcome="1" x="49.9" y="49.9"
           timestamp="2026-05-02T12:30:53.841" ...>
      <Q id="..." qualifier_id="140" value="26.5" />
      <Q id="..." qualifier_id="141" value="40.8" />
      <Q id="..." qualifier_id="56" value="Back" />
    </Event>
    ...
  </Game>
</Games>
```

- `x`/`y` are on Opta's standard **0-100 pitch-length scale** (not meters —
  convert using pitch dimensions from the Second Spectrum metadata, ~104.68m
  × ~65.42m in the sample match, if you need real-world units).
- Qualifiers (`<Q qualifier_id=... value=.../>`) carry event-specific detail.
  The two you'll need constantly: **qualifier_id 140/141 = pass end x/y**
  (present on pass events, `type_id=1`) — that's what turns a pass event
  into a pass *map* (start x/y from the Event attributes, end x/y from
  qualifiers 140/141).
- `outcome="1"` = successful, `"0"` = unsuccessful (works for passes,
  tackles, etc.).
- `player_id`/`team_id` are **Opta's numeric IDs** (e.g. `"215069"`,
  `"33"`), not the padded string form (`"p215069"`/`"t33"`) used in F7 — same
  underlying ID, different string format between the two feeds. Watch for
  this when joining.

**Event type_id values directly confirmed present in real data** (from
`type_id` counts across one real match — not a complete taxonomy, just
what's been seen):

| type_id | Meaning |
|---|---|
| 1 | Pass |
| 4 | Foul |
| 5 | Out (ball out of play) |
| 7 | Tackle |
| 8 | Interception |
| 10 | Save |
| 12 | Clearance |
| 13 | Miss (shot off target) |
| 15 | Attempt Saved (shot saved) |
| 16 | Goal |
| 43 | Deleted event / Good Skill (Opta uses 43 for both historically — verify against outcome/qualifiers if it matters) |
| 44 | Aerial |
| 49 | Ball Recovery |
| 61 | Ball Touch |
| 83 | Offside given |

Shot map = events with `type_id` in `{13, 14, 15, 16}` (Miss, Post, Attempt
Saved, Goal — 14/Post wasn't in this particular match's sample but is
standard Opta taxonomy). This type_id table is **not exhaustive** — Opta's
full F24 taxonomy is a well-known public standard (~80 codes); the ones
above are just what's been personally verified against real data pulled
through this pipeline. Cross-check anything not listed here rather than
guessing.

### Second Spectrum metadata (ASSET_SUBTYPE=40, JSON)

```json
{
  "venueId": "...", "pitchLength": 104.68, "pitchWidth": 65.42, "fps": 25.0,
  "periods": [{"number": 1, "startFrameIdx": 0, "endFrameIdx": 69236, "homeAttPositive": true}, ...],
  "homePlayers": [{"name": "A. Pears", "number": 1, "position": "SUB", "ssiId": "77469842-..."}, ...],
  "awayPlayers": [...]
}
```

`homeAttPositive` tells you which direction the home team attacks in that
period — needed to normalize positions to a consistent "attacking left to
right" orientation across periods/matches.

### Second Spectrum positional tracking (ASSET_SUBTYPE=38) — THE tracking data

**This is the file for heatmaps, average positions, line height, clustering
— everything spatial.** One JSON object per line, 25 lines per second of
match time. Verified real structure (not truncated):

```json
{
  "period": 1, "frameIdx": 0, "gameClock": 0.0, "wallClock": 1774096203560,
  "homePlayers": [
    {"playerId": "159c40f6-...", "number": 24, "xyz": [-7.4, 16.52, 0.0], "speed": 0.0, "optaId": "656699"},
    ...
  ],
  "awayPlayers": [...],
  "ball": {"xyz": [-0.95, 0.06, 0.37], "speed": 18.2},
  "live": true,
  "lastTouch": "home"
}
```

Important fields for your specific asks:
- **`optaId` is on every player entry in every frame** — this is the join
  key back to Opta event data (F24 `player_id`) and to F7 (`PlayerRef` minus
  the `p` prefix). No separate ID-crosswalk lookup needed.
- **`xyz`** — position in meters, pitch-centered (0,0 = center circle,
  matching the `pitchLength`/`pitchWidth` from metadata). This is what you
  aggregate for heatmaps, average position, and line height (mean/median x
  of a unit's players over a window of frames).
- **`live`** (bool) — whether the ball is in play. Filter to `live=true`
  before computing anything positional; frames during stoppages will skew
  averages otherwise.
- **`lastTouch`** (`"home"`/`"away"`) — **this is your possession-phase
  signal**, verified present on every frame. For "average position when in
  possession" vs "out of possession" vs transition, segment frames by
  `lastTouch` matching the team you're analyzing (in possession) vs not
  (out of possession), rather than trying to derive possession state from
  Opta events by timestamp-matching — it's already computed for you here.
- **`ball.xyz`** — ball position, third element is height (z) — useful for
  distinguishing ground passes from aerials/crosses if needed.

**Defensive/attacking line height**: for a team's back line at a given
moment, take the mean (or median, more robust to a defender stepping out of
line) `xyz[0]` (x-coordinate) of the outfield defenders — identify them via
the F7 lineup's `Position="Defender"` players, matched by `optaId`. Do this
separately for `lastTouch` = the team's own possession vs the opponent's, to
get "line height in possession" vs "defensive line height out of
possession" as distinct numbers, which is almost certainly what's actually
wanted rather than one blended average.

### Second Spectrum physical CSVs (ASSET_SUBTYPE 42/43)

Per-player summary (43) — distance, speed-zone breakdown, sprints, **and
TIP/OTIP/BOP splits** (Team In Possession / Opponent Team In Possession /
Ball Out of Play) already broken out per metric:

```csv
ID,Player,Minutes,Distance,Walking,Jogging,Running,High Speed Running,Sprinting,No. of High Intensity Runs,Top Speed,Average Speed,Distance TIP,HSR Distance TIP,Sprint Distance TIP,...,Distance OTIP,...,Distance BOP,...
223821,"Benjamin Cabango",100:44,8942.69,3496.96,3807.87,1198.72,340.6,98.53,38,29.68,5.33,4034.5,95.32,40.12,12,2494.52,...
```

`ID` is the Opta player id. Team-level minute-by-minute splits (42) are a
messier multi-section CSV (thresholds, then a "Minute Splits" table per
team) — worth writing a small dedicated parser rather than a generic
`pd.read_csv()`, the header rows aren't uniform.

## 7. Getting the positional tracking data out of the stage

The `RAW_PAYLOAD` pattern doesn't apply to ASSET_SUBTYPE=38 — those files
are too big for a table cell. They're `PUT` into
`@CAFC_DB.DVMS_RAW.POSITIONAL_STAGE`, gzip-compressed, at a path matching
their `ASSET_KEY`. To check what's there and pull one out:

```sql
LIST @CAFC_DB.DVMS_RAW.POSITIONAL_STAGE;

-- confirm STAGED_AT is set before assuming a file exists:
SELECT FIXTURE_ID, ASSET_KEY, STAGED_AT
FROM CAFC_DB.DVMS_RAW.ASSETS
WHERE ASSET_SUBTYPE = 38 AND STAGED_AT IS NOT NULL;
```

To get the actual file onto disk for local processing (e.g. from a Python
session using `snowflake.connector`):

```python
cur.execute(f"GET @CAFC_DB.DVMS_RAW.POSITIONAL_STAGE/{asset_key} file:///tmp/")
```

That downloads the `.gz` file; decompress and read as JSONL (`gzip.open(...)`,
one `json.loads()` per line) — don't try to `COPY INTO` a table, a single
match is already ~1.5-2M frames × 22 players and isn't a sensible row-per-
frame table shape for this use case; read it as a stream in Python/pandas
instead.

## 8. Getting more data

Only Charlton's last 10 games are loaded (§3). If you need other teams or
matches, **don't attempt to hit the DVMS API yourself** — ask the user to
run the extractor. Context in case it's useful for framing that request:
this pipeline has real, hard-won rate-limiting protections (an earlier
unpaced pull got the account's login temporarily blocked by DVMS's
anti-abuse protection) — a deliberate 1.5s delay per request, one session
per run, and a hard-abort on any 403. Re-running it is safe and cheap
(skips anything already loaded — a repeat run over already-captured data
does zero new work), but it should go through the existing script, not a
fresh implementation:

```bash
# whole competition/season (~550 fixtures, ~90min for small assets):
python -m python.extract.dvms.load_dvms_fixtures

# targeted — a team's last N played games (what was used for Charlton):
python -m python.extract.dvms.load_dvms_fixtures --team-id t33 --last-n-games 10

# then stage positional data for specific fixtures (~20-30s each, ~450-500MB download per match):
python -m python.extract.dvms.load_dvms_positional --fixture-id <fixture_id>
```

Team Opta IDs: query `FIXTURES` for teams already seen
(`SELECT DISTINCT OPTA_HOME_TEAM_ID, HOME_TEAM_NAME FROM ... UNION ...`), or
ask the user — there's no standalone "list all teams" endpoint documented,
team IDs are only visible through fixture data you've already pulled.

## 9. Known gaps / things not to assume

- **No internal player-ID linkage.** Everything here uses Opta's own IDs.
  There's no mapping yet from Opta player IDs to whatever internal
  player/recruitment IDs the wider CAFC data platform uses — if you need to
  join this to recruitment data, that link doesn't exist yet, flag it back
  rather than assuming a join key.
- **Video is never downloaded** — don't expect broadcast/tactical footage,
  only the data feeds above.
- **The DVMS API's own `ready` flag is unreliable** — if you see it exposed
  anywhere, ignore it; content availability was confirmed empirically by
  successful download, not by that flag.
- Second Spectrum "GeniusIQ" in the web UI, "Second Spectrum" in the API —
  same provider, just branded differently in different places.
