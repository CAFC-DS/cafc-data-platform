"""Refresh raw IMPECT match, squad and player metadata for selected iterations.

This is used alongside an event backfill so every event remains joinable to its
fixture, squad and player dimensions.  It deliberately replaces only the
selected iteration's slice of each raw table.
"""
from __future__ import annotations

import argparse

import pandas as pd
from snowflake.connector.pandas_tools import write_pandas

import impect_api as api
from snowflake_loader import flatten_id_mappings, get_connection, json_to_dataframe


TABLES = {
    "matches": ("MATCHES", "ITERATIONID", api.get_matches),
    "squads": ("SQUADS", "ITERATION_ID", api.get_squads),
    "players": ("PLAYERS", "ITERATION_ID", api.get_players),
}


def _replace_iteration(table: str, iteration_column: str, iteration_id: int, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    frame = flatten_id_mappings(frame.copy())
    frame.columns = [column.upper() for column in frame.columns]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DESC TABLE CAFC_DB.IMPECT_RAW.{table}")
            table_columns = {row[0] for row in cur.fetchall()}
            frame = frame[[column for column in frame.columns if column in table_columns]]
            if iteration_column not in frame.columns:
                frame[iteration_column] = iteration_id
            cur.execute(
                f"DELETE FROM CAFC_DB.IMPECT_RAW.{table} WHERE {iteration_column}=%(iteration_id)s",
                {"iteration_id": iteration_id},
            )
        success, _, rows, output = write_pandas(
            conn=conn, df=frame, table_name=table, database="CAFC_DB", schema="IMPECT_RAW",
            auto_create_table=False, overwrite=False,
        )
        if not success:
            raise RuntimeError(f"Could not load {table} for iteration {iteration_id}: {output}")
        conn.commit()
        return rows
    finally:
        conn.close()


def refresh(iteration_ids: set[int]) -> dict[str, int]:
    totals = {name: 0 for name in TABLES}
    for iteration_id in sorted(iteration_ids):
        for name, (table, iteration_column, fetch) in TABLES.items():
            response = fetch(iteration_id)
            frame = json_to_dataframe(response)
            if name != "matches" and not frame.empty:
                frame[iteration_column] = iteration_id
            count = _replace_iteration(table, iteration_column, iteration_id, frame)
            totals[name] += count
            print(f"iteration {iteration_id}: refreshed {count} {name}")
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--iteration-ids", required=True, help="Comma-separated IMPECT iteration IDs.")
    args = parser.parse_args()
    print(refresh({int(value) for value in args.iteration_ids.split(",") if value.strip()}))
