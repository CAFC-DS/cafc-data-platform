"""
Fetch iteration-level (season aggregate) squad KPI averages from Impect API
and load into Snowflake.

Endpoint: GET /v5/customerapi/iterations/{iterationId}/squad-kpis
Table:    CAFC_DB.IMPECT_RAW.ITERATION_SQUAD_KPIS

One API call per iteration (~708 calls total). Serial execution is fine
at this scale (~5-10 minutes).

Usage:
    python load_iteration_squad_kpis.py                    # all iterations
    python load_iteration_squad_kpis.py --limit-iterations 5  # smoke test
"""
import argparse

import pandas as pd

from impect_api import get_iterations, get_iteration_squad_kpis
from snowflake_loader import flatten_id_mappings, json_to_dataframe, load_to_snowflake

TABLE_NAME = "ITERATION_SQUAD_KPIS"


def run(limit_iterations=None):
    print("Fetching iterations…")
    iterations_response = get_iterations()
    iteration_ids = [row["id"] for row in iterations_response.get("data", [])]
    if limit_iterations:
        iteration_ids = iteration_ids[:limit_iterations]
    print(f"Fetching squad KPIs for {len(iteration_ids)} iterations")

    all_frames = []
    errors = []

    for i, iteration_id in enumerate(iteration_ids, start=1):
        try:
            response = get_iteration_squad_kpis(iteration_id)
            df = json_to_dataframe(response)
            if not df.empty:
                df["ITERATION_ID"] = iteration_id
                all_frames.append(df)
        except Exception as exc:  # noqa: BLE001
            errors.append((iteration_id, str(exc)))
        if i % 50 == 0 or i == len(iteration_ids):
            print(f"  progress: {i}/{len(iteration_ids)}  (errors so far: {len(errors)})")

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
    combined_df = flatten_id_mappings(combined_df)
    print(f"Total rows to load: {len(combined_df)}")
    load_to_snowflake(combined_df, TABLE_NAME, overwrite=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--limit-iterations", type=int, default=None,
        help="Cap the number of iterations processed. Useful for smoke-tests.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(limit_iterations=args.limit_iterations)
