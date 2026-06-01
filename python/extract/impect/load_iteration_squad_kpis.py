"""
Fetch iteration-level (season aggregate) squad KPI averages from Impect API
and load into Snowflake.

Endpoint: GET /v5/customerapi/iterations/{iterationId}/squad-kpis
Table:    CAFC_DB.IMPECT_RAW.ITERATION_SQUAD_KPIS

Output shape: one row per (iteration, squad, kpi). Mirrors the existing
flat CHAMPIONSHIP_PLAYER_KPIS pattern.

Columns landed: ITERATION_ID, SQUAD_ID, MATCHES, KPI_ID, VALUE.

Usage:
    python load_iteration_squad_kpis.py                       # all iterations
    python load_iteration_squad_kpis.py --iteration-id 1410   # single (testing)
    python load_iteration_squad_kpis.py --limit-iterations 5  # first 5
"""
import argparse

import pandas as pd

from impect_api import get_iterations, get_iteration_squad_kpis
from snowflake_loader import load_to_snowflake

TABLE_NAME = "ITERATION_SQUAD_KPIS"


def _flatten(records, iteration_id):
    """Explode the nested `kpis` array into one row per (squad, kpi)."""
    if not records:
        return pd.DataFrame()
    df = pd.json_normalize(
        records,
        record_path="kpis",
        meta=["squadId", "matches"],
    )
    df["iteration_id"] = iteration_id
    df = df.rename(columns={
        "kpiId":   "kpi_id",
        "value":   "value",
        "squadId": "squad_id",
    })
    return df[["iteration_id", "squad_id", "matches", "kpi_id", "value"]]


def run(iteration_id=None, limit_iterations=None, include_womens=False):
    if iteration_id is not None:
        iteration_ids = [iteration_id]
        print(f"Targeted run: iteration_id={iteration_id}")
    else:
        print("Fetching iterations…")
        iterations_response = get_iterations()
        iterations = iterations_response.get("data", [])
        # Women's competitions are excluded platform-wide (decided 2026-05-31);
        # skip them by default so bulk extracts don't pull data dbt would filter.
        if not include_womens:
            before = len(iterations)
            iterations = [it for it in iterations
                          if (it.get("competition") or {}).get("gender") == "MALE"]
            print(f"  excluding women's competitions: {before - len(iterations)} dropped, "
                  f"{len(iterations)} male iterations remain")
        iteration_ids = [row["id"] for row in iterations]
        if limit_iterations:
            iteration_ids = iteration_ids[:limit_iterations]
        print(f"Fetching squad KPIs for {len(iteration_ids)} iterations")

    all_frames = []
    errors = []
    empty_iterations = 0

    for i, iter_id in enumerate(iteration_ids, start=1):
        try:
            response = get_iteration_squad_kpis(iter_id)
            data = response.get("data", [])
            if not data:
                empty_iterations += 1
                continue
            df = _flatten(data, iter_id)
            if not df.empty:
                all_frames.append(df)
        except Exception as exc:  # noqa: BLE001
            errors.append((iter_id, str(exc)))
        if i % 50 == 0 or i == len(iteration_ids):
            print(f"  progress: {i}/{len(iteration_ids)}  empty: {empty_iterations}  errors: {len(errors)}")

    if errors:
        print(f"\n{len(errors)} iteration(s) failed:")
        for iter_id, msg in errors[:10]:
            print(f"  iteration {iter_id}: {msg}")
        if len(errors) > 10:
            print(f"  … and {len(errors) - 10} more")

    if not all_frames:
        print("No data retrieved")
        return

    combined_df = pd.concat(all_frames, ignore_index=True)
    print(f"Total rows to load: {len(combined_df)}  (from {len(all_frames)} non-empty iterations)")
    load_to_snowflake(combined_df, TABLE_NAME, overwrite=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--iteration-id", type=int, default=None,
        help="Run for a specific iteration only (smoke-testing). Overrides --limit-iterations.",
    )
    parser.add_argument(
        "--limit-iterations", type=int, default=None,
        help="Cap the number of iterations processed.",
    )
    parser.add_argument(
        "--include-womens", action="store_true",
        help="Override the platform-wide women's-competition exclusion and "
             "fetch women's iterations too. Off by default.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(iteration_id=args.iteration_id, limit_iterations=args.limit_iterations,
        include_womens=args.include_womens)
