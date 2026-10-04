"""Staged plan for the IMPECT historical event backfill.

A plan splits the queued matches into *stages*: season blocks, newest first
(a block pairs the split-year season with its calendar-year twin, e.g.
"26/27+2026"), and within each block one stage per geographic group.  The
Railway supervisor (railway_backfill.py, BACKFILL_PLAN) runs the stages in
order, so a run can be paused or stopped at a block boundary, credits can be
checked per block via the stage's QUERY_TAG, and an unfinished stage resumes
from the queue.

The plan only lists iteration ids.  What is still to do is always read live
from the queue, so a plan never goes stale.  Regenerate it to pick up new
iterations or to change the seasons.

Usage (read-only against Snowflake; needs a role that can read the queue):
    python backfill_plan.py --show
    python backfill_plan.py --write backfill_plans/five_seasons_men.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Iterable

# Newest first.  Each block pairs a split-year season with its calendar-year twin.
SEASON_BLOCKS: list[tuple[str, list[str]]] = [
    ("26/27+2026", ["26/27", "2026"]),
    ("25/26+2025", ["25/26", "2025"]),
    ("24/25+2024", ["24/25", "2024"]),
    ("23/24+2023", ["23/24", "2023"]),
    ("22/23+2022", ["22/23", "2022"]),
]

# Geographic groups in run order; values are IMPECT COMPETITION.COUNTRYID values
# (IMPECT_RAW.COUNTRIES.ID).  The country names in comments are for people.
REGION_GROUPS: list[tuple[str, list[int]]] = [
    ("Nordics & Baltics", [393, 462, 445, 533, 475, 571, 565]),          # Norway, Sweden, Denmark, Iceland, Finland, Lithuania, Latvia
    ("British Isles", [549, 559, 532, 561]),                             # England, Scotland, Ireland, Northern Ireland
    ("Germany, Austria & Switzerland", [466, 398, 464]),
    ("Western Europe", [494, 384, 376]),                                 # France, Netherlands, Belgium
    ("Southern Europe", [535, 491, 430, 500]),                           # Italy, Spain, Portugal, Greece
    ("Central & Eastern Europe, Balkans & Turkey",
     [427, 522, 485, 528, 449, 527, 497, 524, 443, 576, 488, 448, 414]),  # Poland, Czechia, Slovakia, Hungary, Russia, Ukraine, Georgia, Turkey, Croatia, Serbia, Slovenia, Romania, Bulgaria
    ("North & Central America", [544, 349, 550, 442]),                   # USA, Mexico, Canada, Costa Rica
    ("South America", [412, 405, 553, 529, 470, 423, 421, 439, 409, 542]),  # Brazil, Argentina, Colombia, Uruguay, Ecuador, Peru, Paraguay, Chile, Bolivia, Venezuela
    ("Asia, Middle East & Oceania", [537, 440, 556, 460, 543, 432, 369, 510, 534]),  # Japan, China, South Korea, Saudi Arabia, UAE, Qatar, Australia, Thailand, Israel
    ("Africa", [493, 595]),                                              # South Africa, Morocco
    ("International & continental", [346]),                              # IMPECT's "unknown" country: continental cups, internationals, friendlies
]
OTHER_GROUP = "Other (unmapped country)"


def _group_of(country_id: int | None) -> str:
    for name, country_ids in REGION_GROUPS:
        if country_id in country_ids:
            return name
    return OTHER_GROUP


def build_plan(rows: Iterable[dict[str, Any]], generated_at: str | None = None) -> dict[str, Any]:
    """Build the plan from per-iteration rows.

    Each row: iteration_id, season, country_id, matches, todo.  Rows whose season
    is in no block are ignored; countries in no group go to a trailing "Other"
    group so nothing is silently dropped.
    """
    block_of_season = {season: label for label, seasons in SEASON_BLOCKS for season in seasons}
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        block = block_of_season.get(row["season"])
        if block is None:
            continue
        group = _group_of(row["country_id"])
        cell = cells.setdefault((block, group), {"iteration_ids": set(), "matches": 0, "todo": 0})
        cell["iteration_ids"].add(int(row["iteration_id"]))
        cell["matches"] += int(row["matches"])
        cell["todo"] += int(row["todo"])

    group_order = [name for name, _ in REGION_GROUPS] + [OTHER_GROUP]
    blocks = []
    for label, seasons in SEASON_BLOCKS:
        stages = []
        for group in group_order:
            cell = cells.get((label, group))
            if cell:
                stages.append({"group": group, "iteration_ids": sorted(cell["iteration_ids"]),
                               "matches": cell["matches"], "todo": cell["todo"]})
        blocks.append({"label": label, "seasons": seasons, "stages": stages})
    return {
        "name": "five-seasons-men",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "blocks": blocks,
    }


def load_plan(path: str) -> dict[str, Any]:
    with open(path) as handle:
        plan = json.load(handle)
    if not isinstance(plan.get("blocks"), list):
        raise ValueError(f"{path} is not a backfill plan (no 'blocks' list)")
    return plan


def select_stages(plan: dict[str, Any], block_labels: list[str] | None = None) -> list[dict[str, Any]]:
    """Flatten the plan into ordered stages, optionally limited to some blocks.

    Unknown block labels raise, so a typo cannot silently run nothing (or everything).
    """
    known = [block["label"] for block in plan["blocks"]]
    if block_labels:
        unknown = [label for label in block_labels if label not in known]
        if unknown:
            raise ValueError(f"unknown plan block(s) {unknown}; plan has {known}")
    stages = []
    for block in plan["blocks"]:
        if block_labels and block["label"] not in block_labels:
            continue
        for stage in block["stages"]:
            stages.append({"block": block["label"], "group": stage["group"],
                           "iteration_ids": list(stage["iteration_ids"]),
                           "matches": stage.get("matches"), "todo": stage.get("todo")})
    return stages


def fill_missing_countries(rows: list[dict[str, Any]], api_iterations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill country ids the metadata table does not know yet (iterations new since the last discovery)."""
    by_id = {int(it["id"]): (it.get("competition") or {}).get("countryId") for it in api_iterations}
    for row in rows:
        if row["country_id"] is None and by_id.get(int(row["iteration_id"])) is not None:
            row["country_id"] = int(by_id[int(row["iteration_id"])])
    return rows


def _fetch_rows() -> list[dict[str, Any]]:
    from snowflake_loader import get_connection  # imported lazily so the pure functions need no Snowflake

    seasons = [season for _, group in SEASON_BLOCKS for season in group]
    placeholders = ", ".join(f"%(s{index})s" for index in range(len(seasons)))
    params = {f"s{index}": season for index, season in enumerate(seasons)}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT q.ITERATION_ID, q.SEASON, i."COMPETITION.COUNTRYID", COUNT(*),
                   COUNT_IF(q.STATUS NOT IN ('SUCCESS', 'NO_EVENT_DATA'))
            FROM CAFC_DB.CORE.IMPECT_EVENT_BACKFILL_QUEUE q
            LEFT JOIN CAFC_DB.IMPECT_RAW.ITERATIONS i ON i.ID = q.ITERATION_ID
            WHERE q.SEASON IN ({placeholders})
            GROUP BY 1, 2, 3
            """,
            params,
        )
        rows = [{"iteration_id": r[0], "season": r[1], "country_id": int(r[2]) if r[2] is not None else None,
                 "matches": r[3], "todo": r[4]} for r in cur.fetchall()]
    finally:
        conn.close()
    if any(row["country_id"] is None for row in rows):
        import impect_api  # only needed when some iterations are newer than the metadata table
        response = impect_api.get_iterations()
        rows = fill_missing_countries(rows, response.get("data", []) if isinstance(response, dict) else [])
    return rows


def format_summary(plan: dict[str, Any]) -> str:
    lines = [f"plan {plan['name']} (generated {plan['generated_at']})"]
    total_matches = total_todo = 0
    for block in plan["blocks"]:
        block_matches = sum(stage["matches"] for stage in block["stages"])
        block_todo = sum(stage["todo"] for stage in block["stages"])
        total_matches += block_matches
        total_todo += block_todo
        lines.append(f"\n{block['label']}: {block_todo:,} to do of {block_matches:,}")
        for stage in block["stages"]:
            lines.append(f"  {stage['group']:<45} {len(stage['iteration_ids']):>4} iterations  "
                         f"{stage['todo']:>7,} to do of {stage['matches']:>7,}")
    lines.append(f"\ntotal: {total_todo:,} to do of {total_matches:,}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", metavar="PATH", help="Write the plan JSON to PATH.")
    parser.add_argument("--show", action="store_true", help="Print a summary of the plan.")
    args = parser.parse_args(argv)
    if not (args.write or args.show):
        parser.error("pass --show and/or --write PATH")
    plan = build_plan(_fetch_rows())
    others = [stage for block in plan["blocks"] for stage in block["stages"] if stage["group"] == OTHER_GROUP]
    if others:
        print(f"WARNING: {len(others)} stage(s) hold countries with no group; add them to REGION_GROUPS",
              file=sys.stderr)
    if args.show:
        print(format_summary(plan))
    if args.write:
        with open(args.write, "w") as handle:
            json.dump(plan, handle, indent=1)
            handle.write("\n")
        print(f"wrote {args.write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
