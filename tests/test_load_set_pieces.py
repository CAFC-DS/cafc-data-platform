import json
from pathlib import Path
import sys


IMPECT_DIR = Path(__file__).resolve().parents[1] / "python" / "extract" / "impect"
sys.path.insert(0, str(IMPECT_DIR))

import load_set_pieces as loader  # noqa: E402


SAMPLE_PHASE = {
    "id": 90147817,
    "matchId": 267839,
    "startTimeInSec": 329.664,
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
            "freeKickEndZone": None,
            "freeKickType": None,
            "goalKickEndZone": None,
            "goalKickType": None,
            "throwInEndZone": None,
            "throwInType": None,
            "mainEventPlayerId": 5632,
            "mainEventOutcome": "UNSUCCESSFUL",
            "passReceiverId": 89548,
            "ballTrajectory": "INSWINGING",
            "firstTouchPlayerId": None,
            "firstTouchWon": False,
            "indirectHeader": "NONE",
            "secondTouchPlayerId": 103502,
            "secondTouchWon": True,
            "secondTouchEndZone": "NEAR_POST_WIDE",
            "aggregates": {"SHOT_XG": 0.0569},
        }
    ],
}


def test_payload_row_flattens_one_row_per_sub_phase():
    rows = loader.payload_row([SAMPLE_PHASE], 267839, 2114, run_id=7)

    assert len(rows) == 1
    row = rows[0]
    assert row["MATCH_ID"] == 267839
    assert row["ITERATION_ID"] == 2114
    assert row["SET_PIECE_ID"] == 90147817
    assert row["SUB_PHASE_ID"] == 95597453
    assert row["CORNER_TYPE"] == "CORNER_NEAR_POST"
    assert row["BALL_TRAJECTORY"] == "INSWINGING"
    assert row["SECOND_TOUCH_WON"] is True
    assert json.loads(row["AGGREGATES"])["SHOT_XG"] == 0.0569
    assert json.loads(row["RAW_PHASE"])["id"] == 90147817
    assert row["INGESTION_RUN_ID"] == 7
    assert set(row.keys()) == set(loader.COLUMNS)


def test_payload_row_handles_multiple_sub_phases():
    phase = dict(SAMPLE_PHASE)
    phase["setPieceSubPhase"] = [SAMPLE_PHASE["setPieceSubPhase"][0], {**SAMPLE_PHASE["setPieceSubPhase"][0], "id": 99, "index": 1}]

    rows = loader.payload_row([phase], 267839, 2114)

    assert len(rows) == 2
    assert {r["SUB_PHASE_ID"] for r in rows} == {95597453, 99}


def test_payload_row_empty_phases_produces_no_rows():
    assert loader.payload_row([], 267839, 2114) == []


def test_fetch_and_load_uses_unwrapped_payload(monkeypatch):
    saved = []
    monkeypatch.setattr(loader.api, "get_match_set_pieces", lambda match_id: {"data": [SAMPLE_PHASE]})
    monkeypatch.setattr(loader, "upsert", saved.append)

    loader.fetch_and_load(267839, 2114, run_id=8)

    assert len(saved[0]) == 1
    assert saved[0][0]["MATCH_ID"] == 267839
    assert saved[0][0]["INGESTION_RUN_ID"] == 8


def test_backfill_continues_after_one_match_fails(monkeypatch, capsys):
    monkeypatch.setattr(loader, "missing_set_piece_matches", lambda limit, lane=0, lanes=1: [(1, 10), (2, 10)])
    loaded = []

    def fake_fetch(match_id, iteration_id, run_id=None, conn=None):
        if match_id == 1:
            raise RuntimeError("temporary failure")
        loaded.append((match_id, iteration_id))

    monkeypatch.setattr(loader, "fetch_and_load", fake_fetch)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(loader, "get_connection", lambda: FakeConnection())

    result = loader.backfill(limit=None, pause_seconds=0, dry_run=False)

    assert result == {"candidates": 2, "loaded": 1, "failed": 1}
    assert loaded == [(2, 10)]
    assert "lane 0/1 progress: 2/2 loaded=1 failed=1" in capsys.readouterr().out
