"""
Download Championship match-level data for the last N seasons from Impect API.

Must-have endpoints:
- /matches/{matchId}
- /matches/{matchId}/player-kpis
- /matches/{matchId}/squad-kpis

Outputs both raw JSONL / CSV files and Snowflake tables under IMPECT_RAW.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from impect_api import (
    get_iterations,
    get_matches,
    get_match_info,
    get_match_player_kpis,
    get_match_squad_kpis,
)
from snowflake_loader import load_to_snowflake

DEFAULT_COMPETITION_ID = 41
DEFAULT_SEASON_COUNT = 5
DEFAULT_OUTPUT_DIR = Path("downloads/championship_last5")
MATCH_INDEX_TABLE = "CHAMPIONSHIP_MATCH_INDEX"
MATCH_INFO_TABLE = "CHAMPIONSHIP_MATCH_INFO"
PLAYER_KPIS_TABLE = "CHAMPIONSHIP_PLAYER_KPIS"
SQUAD_KPIS_TABLE = "CHAMPIONSHIP_SQUAD_KPIS"
DEFAULT_MAX_WORKERS = 8
DEFAULT_BATCH_SIZE = 100


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download match-info, player-kpis and squad-kpis for Championship seasons."
    )
    parser.add_argument(
        "--competition-id",
        type=int,
        default=DEFAULT_COMPETITION_ID,
        help="Competition ID to fetch. Defaults to 41 (Championship).",
    )
    parser.add_argument(
        "--season-count",
        type=int,
        default=DEFAULT_SEASON_COUNT,
        help="Number of most recent seasons to fetch. Defaults to 5.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory to write raw JSONL and flattened CSV outputs.",
    )
    parser.add_argument(
        "--limit-matches",
        type=int,
        help="Optional cap on number of matches fetched, useful for testing.",
    )
    parser.add_argument(
        "--skip-snowflake",
        action="store_true",
        help="Write local files only and skip loading IMPECT_RAW tables.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help="Number of parallel workers for match-level API calls.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Number of matches to process per write/load batch.",
    )
    return parser.parse_args()


def season_sort_key(season_value: str) -> tuple[int, str]:
    season = str(season_value).strip()
    if "/" in season:
        try:
            start_year = int(season.split("/")[0])
            if start_year < 100:
                start_year += 2000
            return start_year, season
        except ValueError:
            return -1, season

    try:
        return int(season), season
    except ValueError:
        return -1, season


def resolve_iterations(competition_id: int, season_count: int) -> list[dict]:
    print("Fetching iterations...")
    iterations_response = get_iterations()
    all_iterations = iterations_response.get("data", [])

    filtered = [
        iteration
        for iteration in all_iterations
        if iteration.get("competition", {}).get("id") == competition_id
    ]
    filtered.sort(key=lambda row: season_sort_key(row.get("season", "")), reverse=True)

    selected = filtered[:season_count]
    if not selected:
        raise ValueError(f"No iterations found for competition {competition_id}")

    print(
        f"Resolved {len(selected)} seasons for competition {competition_id}: "
        + ", ".join(f"{row['season']} (iteration {row['id']})" for row in selected)
    )
    return selected


def fetch_match_index(iterations: list[dict], limit_matches: int | None = None) -> pd.DataFrame:
    rows = []

    for iteration in iterations:
        iteration_id = iteration["id"]
        season = iteration.get("season")
        competition = iteration.get("competition", {}).get("name")
        print(f"Fetching matches for {competition} {season} (iteration {iteration_id})...")
        response = get_matches(iteration_id)

        for match in response.get("data", []):
            rows.append(
                {
                    "competitionId": iteration.get("competition", {}).get("id"),
                    "competitionName": competition,
                    "season": season,
                    "iterationId": iteration_id,
                    "matchId": match.get("id"),
                    "scheduledDate": match.get("scheduledDate"),
                    "lastCalculationDate": match.get("lastCalculationDate"),
                    "homeSquadId": match.get("homeSquadId"),
                    "awaySquadId": match.get("awaySquadId"),
                    "matchDayIndex": (match.get("matchDay") or {}).get("index"),
                }
            )

    match_index = pd.DataFrame(rows).drop_duplicates(subset=["matchId"]).reset_index(drop=True)
    if limit_matches is not None:
        match_index = match_index.head(limit_matches).copy()

    print(f"Resolved {len(match_index)} matches to download")
    return match_index


def write_jsonl(path: Path, records: list[dict]):
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=True))
            handle.write("\n")


def append_jsonl(path: Path, records: list[dict]):
    if not records:
        return

    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=True))
            handle.write("\n")


def append_csv(path: Path, df: pd.DataFrame):
    if df.empty:
        return

    df.to_csv(path, mode="a", header=not path.exists(), index=False)


def reset_output_files(output_dir: Path):
    for filename in (
        "match_info_raw.jsonl",
        "player_kpis_raw.jsonl",
        "squad_kpis_raw.jsonl",
        "match_info.csv",
        "player_kpis.csv",
        "squad_kpis.csv",
    ):
        target = output_dir / filename
        if target.exists():
            target.unlink()


def unwrap_payload(response: dict) -> dict:
    payload = response.get("data", response)
    return payload if isinstance(payload, dict) else {}


def flatten_match_info(payload: dict, match_context: dict) -> dict:
    row = dict(match_context)
    row.update(
        {
            "dateTime": payload.get("dateTime"),
            "lastCalculationDate": payload.get("lastCalculationDate"),
            "stadiumId": payload.get("stadiumId"),
            "squadHomeId": (payload.get("squadHome") or {}).get("id"),
            "squadAwayId": (payload.get("squadAway") or {}).get("id"),
            "squadHomeCoachId": (payload.get("squadHome") or {}).get("coachId"),
            "squadAwayCoachId": (payload.get("squadAway") or {}).get("coachId"),
            "squadHomeStartingFormation": (payload.get("squadHome") or {}).get("startingFormation"),
            "squadAwayStartingFormation": (payload.get("squadAway") or {}).get("startingFormation"),
            "squadHomePlayersJson": json.dumps((payload.get("squadHome") or {}).get("players", []), ensure_ascii=True),
            "squadAwayPlayersJson": json.dumps((payload.get("squadAway") or {}).get("players", []), ensure_ascii=True),
            "squadHomeStartingPositionsJson": json.dumps((payload.get("squadHome") or {}).get("startingPositions", []), ensure_ascii=True),
            "squadAwayStartingPositionsJson": json.dumps((payload.get("squadAway") or {}).get("startingPositions", []), ensure_ascii=True),
            "squadHomeSubstitutionsJson": json.dumps((payload.get("squadHome") or {}).get("substitutions", []), ensure_ascii=True),
            "squadAwaySubstitutionsJson": json.dumps((payload.get("squadAway") or {}).get("substitutions", []), ensure_ascii=True),
            "squadHomeFormationsJson": json.dumps((payload.get("squadHome") or {}).get("formations", []), ensure_ascii=True),
            "squadAwayFormationsJson": json.dumps((payload.get("squadAway") or {}).get("formations", []), ensure_ascii=True),
        }
    )
    return row


def flatten_player_kpis(payload: dict, match_context: dict) -> list[dict]:
    rows = []

    for squad_side in ("squadHome", "squadAway"):
        squad = payload.get(squad_side) or {}
        squad_id = squad.get("id")

        for player in squad.get("players", []):
            kpis = player.get("kpis") or []
            if not kpis:
                rows.append(
                    {
                        **match_context,
                        "squadSide": squad_side,
                        "squadId": squad_id,
                        "playerId": player.get("id"),
                        "position": player.get("position"),
                        "playDuration": player.get("playDuration"),
                        "matchShare": player.get("matchShare"),
                        "kpiId": None,
                        "value": None,
                    }
                )
                continue

            for kpi in kpis:
                rows.append(
                    {
                        **match_context,
                        "squadSide": squad_side,
                        "squadId": squad_id,
                        "playerId": player.get("id"),
                        "position": player.get("position"),
                        "playDuration": player.get("playDuration"),
                        "matchShare": player.get("matchShare"),
                        "kpiId": kpi.get("kpiId"),
                        "value": kpi.get("value"),
                    }
                )

    return rows


def flatten_squad_kpis(payload: dict, match_context: dict) -> list[dict]:
    rows = []

    for squad_side in ("squadHome", "squadAway"):
        squad = payload.get(squad_side) or {}
        squad_id = squad.get("id")

        for kpi in squad.get("kpis", []):
            rows.append(
                {
                    **match_context,
                    "squadSide": squad_side,
                    "squadId": squad_id,
                    "kpiId": kpi.get("kpiId"),
                    "value": kpi.get("value"),
                }
            )

    return rows


def chunk_rows(rows: list[dict], batch_size: int):
    for start in range(0, len(rows), batch_size):
        yield rows[start:start + batch_size]


def fetch_match_payload(row) -> dict | None:
    match_context = {
        "competitionId": row.competitionId,
        "competitionName": row.competitionName,
        "season": row.season,
        "iterationId": row.iterationId,
        "matchId": row.matchId,
        "scheduledDate": row.scheduledDate,
    }

    try:
        match_info_response = get_match_info(row.matchId)
        player_kpis_response = get_match_player_kpis(row.matchId)
        squad_kpis_response = get_match_squad_kpis(row.matchId)
    except Exception as exc:
        print(f"  Warning: could not fetch match {row.matchId}: {exc}")
        return None

    return {
        "matchContext": match_context,
        "matchInfo": unwrap_payload(match_info_response),
        "playerKpis": unwrap_payload(player_kpis_response),
        "squadKpis": unwrap_payload(squad_kpis_response),
    }


def process_match_batch(batch_rows: list, output_dir: Path, load_to_snowflake_enabled: bool, first_batch: bool, max_workers: int):
    fetched_payloads = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_match_payload, row): row.matchId
            for row in batch_rows
        }

        for future in as_completed(futures):
            payload = future.result()
            if payload is not None:
                fetched_payloads.append(payload)

    fetched_payloads.sort(key=lambda item: item["matchContext"]["matchId"])

    raw_match_info = []
    raw_player_kpis = []
    raw_squad_kpis = []
    flat_match_info = []
    flat_player_kpis = []
    flat_squad_kpis = []

    for payload in fetched_payloads:
        match_context = payload["matchContext"]
        match_info = payload["matchInfo"]
        player_kpis = payload["playerKpis"]
        squad_kpis = payload["squadKpis"]

        raw_match_info.append({"matchContext": match_context, "payload": match_info})
        raw_player_kpis.append({"matchContext": match_context, "payload": player_kpis})
        raw_squad_kpis.append({"matchContext": match_context, "payload": squad_kpis})

        flat_match_info.append(flatten_match_info(match_info, match_context))
        flat_player_kpis.extend(flatten_player_kpis(player_kpis, match_context))
        flat_squad_kpis.extend(flatten_squad_kpis(squad_kpis, match_context))

    match_info_df = pd.DataFrame(flat_match_info)
    player_kpis_df = pd.DataFrame(flat_player_kpis)
    squad_kpis_df = pd.DataFrame(flat_squad_kpis)

    append_jsonl(output_dir / "match_info_raw.jsonl", raw_match_info)
    append_jsonl(output_dir / "player_kpis_raw.jsonl", raw_player_kpis)
    append_jsonl(output_dir / "squad_kpis_raw.jsonl", raw_squad_kpis)

    append_csv(output_dir / "match_info.csv", match_info_df)
    append_csv(output_dir / "player_kpis.csv", player_kpis_df)
    append_csv(output_dir / "squad_kpis.csv", squad_kpis_df)

    if load_to_snowflake_enabled:
        overwrite = first_batch
        load_to_snowflake(match_info_df.copy(), MATCH_INFO_TABLE, overwrite=overwrite)
        load_to_snowflake(player_kpis_df.copy(), PLAYER_KPIS_TABLE, overwrite=overwrite)
        load_to_snowflake(squad_kpis_df.copy(), SQUAD_KPIS_TABLE, overwrite=overwrite)

    return len(fetched_payloads)


def run():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    iterations = resolve_iterations(args.competition_id, args.season_count)
    match_index = fetch_match_index(iterations, args.limit_matches)
    reset_output_files(output_dir)
    match_index.to_csv(output_dir / "match_index.csv", index=False)

    if args.skip_snowflake:
        print("Skipping Snowflake load by request")
    else:
        print("Loading Championship match index into Snowflake IMPECT_RAW schema...")
        load_to_snowflake(match_index.copy(), MATCH_INDEX_TABLE, overwrite=True)

    total_batches = (len(match_index) + args.batch_size - 1) // args.batch_size
    processed_matches = 0

    for batch_number, batch in enumerate(
        chunk_rows(list(match_index.itertuples(index=False)), args.batch_size),
        start=1
    ):
        print(
            f"Processing batch {batch_number}/{total_batches} "
            f"({len(batch)} matches, max_workers={args.max_workers})..."
        )
        batch_successes = process_match_batch(
            batch_rows=batch,
            output_dir=output_dir,
            load_to_snowflake_enabled=not args.skip_snowflake,
            first_batch=(batch_number == 1),
            max_workers=args.max_workers,
        )
        processed_matches += batch_successes
        print(f"Completed batch {batch_number}/{total_batches}; fetched {processed_matches} matches so far")

    print(f"Files written to {output_dir.resolve()}")


if __name__ == "__main__":
    run()
