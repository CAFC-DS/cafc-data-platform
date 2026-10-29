# Refreshing the data-scoring job as backfill stages finish

The IMPECT backfill loads event data in **stages** (a season block x a geographic group, e.g.
`26/27+2026 | Nordics & Baltics`). When a stage finishes, the leagues and seasons in it are fully loaded
and ready to score. `python/tools/backfill_stage_status.py` tells you which stages are ready, so a
scoring job can pull what is new instead of being pushed.

## Which stages are new?

From `python/tools/` (uses the same Snowflake credentials as the other extractors; read-only):

```bash
python backfill_stage_status.py --blocks 26/27+2026                         # table of every stage's status
python backfill_stage_status.py --since 2026-10-04T18:30:00 --json          # only stages completed after the watermark
```

Each stage in the JSON has `block`, `group`, `status`, `completed_at`, load counts, and `iterations`:
`[{iteration_id, competition, season, matches_loaded}]`. Those iterations are what to (re)score. The data is in
`CAFC_DB.IMPECT_RAW.EVENTS` (events) and `CAFC_DB.IMPECT_RAW.MATCH_INFO` (line-ups), keyed by `ITERATION_ID`/`MATCH_ID`.

- `completed_at` is in the queue's clock (Europe/London), without a zone. Keep the **latest `completed_at` you
  processed** as your watermark and pass it as `--since` next time.
- Only `complete` stages are listed by `--since`. A stage that is `in_progress` is still loading; scoring it now
  would score a partial league.
- A live season (`2026`, `26/27`) can **reopen** when new matches are added to the queue: a stage you scored before
  may become `in_progress` again and later complete again with a newer `completed_at`, which is the cue to re-score it.
- Matches counted under `no_event_data` genuinely have no events at IMPECT (IMPECT returns an empty list), so they
  contribute nothing to scoring.

## Standing instruction for the local Claude session

> Whenever I ask you to refresh the scoring, or when a message says backfill stages have completed: run
> `python backfill_stage_status.py --since <watermark> --json` from `python/tools/` of the cafc-data-platform
> checkout, re-run the data-scoring job for the `iterations` it returns (only those), refresh the data-scoring
> artifact, then record the latest `completed_at` you handled as the new watermark (keep it in a small local file).
> Do not score stages that are not `complete`.

The backfill supervisor's progress notes name the stages as they finish; the same command gives the authoritative list.
