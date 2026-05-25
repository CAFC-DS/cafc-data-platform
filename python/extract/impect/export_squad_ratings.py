"""
Fetch squad ratings for all iterations from Impect API and save locally as CSV.
Output file: squad_ratings.csv
"""
import argparse
import pandas as pd
from impect_api import get_iterations, get_squad_ratings

OUTPUT_FILE = "squad_ratings.csv"


def squad_ratings_to_dataframe(response: dict, fallback_iteration_id: int) -> pd.DataFrame:
    """Flatten squad ratings into one row per iteration/date/squad."""
    payload = response.get('data', response)
    rows = []

    if isinstance(payload, dict):
        rows.extend(_flatten_entries(
            payload.get('squadRatingsEntries', []),
            payload.get('iterationId', fallback_iteration_id)
        ))
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue

            if 'squadRatingsEntries' in item:
                rows.extend(_flatten_entries(
                    item.get('squadRatingsEntries', []),
                    item.get('iterationId', fallback_iteration_id)
                ))
            else:
                rows.extend(_flatten_entries([item], fallback_iteration_id))

    return pd.DataFrame(rows)


def _flatten_entries(entries, iteration_id: int):
    rows = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        rating_date = entry.get('date')
        squad_ratings = entry.get('squadRatings', [])
        if not isinstance(squad_ratings, list):
            continue

        for squad_rating in squad_ratings:
            if not isinstance(squad_rating, dict):
                continue

            row = {
                'iterationId': iteration_id,
                'date': rating_date,
                'squadId': squad_rating.get('squadId'),
                'value': squad_rating.get('value')
            }

            for key, value in squad_rating.items():
                if key not in row:
                    row[key] = value

            rows.append(row)

    return rows


def parse_args():
    parser = argparse.ArgumentParser(description="Export squad ratings to CSV")
    parser.add_argument(
        "--iteration-ids",
        help="Comma-separated iteration IDs to fetch. Defaults to all iterations."
    )
    return parser.parse_args()


def _resolve_iteration_ids(raw_iteration_ids: str | None) -> list[int]:
    if not raw_iteration_ids:
        print("Fetching iterations...")
        iterations_response = get_iterations()
        iteration_ids = [row['id'] for row in iterations_response.get('data', [])]
        print(f"Found {len(iteration_ids)} iterations")
        return iteration_ids

    iteration_ids = [
        int(part.strip())
        for part in raw_iteration_ids.split(",")
        if part.strip()
    ]
    print(f"Using {len(iteration_ids)} requested iterations")
    return iteration_ids


def run(iteration_ids: list[int] | None = None):
    if iteration_ids is None:
        args = parse_args()
        iteration_ids = _resolve_iteration_ids(args.iteration_ids)

    all_frames = []

    for iteration_id in iteration_ids:
        print(f"  Fetching squad ratings for iteration {iteration_id}...")
        try:
            response = get_squad_ratings(iteration_id)
            df = squad_ratings_to_dataframe(response, iteration_id)
            if not df.empty:
                all_frames.append(df)
        except Exception as e:
            print(f"  Warning: Could not fetch squad ratings for iteration {iteration_id}: {e}")
            continue

    if not all_frames:
        print("No squad rating data retrieved")
        return

    combined_df = pd.concat(all_frames, ignore_index=True)
    combined_df.to_csv(OUTPUT_FILE, index=False)
    print(f"Saved {len(combined_df)} rows to {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
