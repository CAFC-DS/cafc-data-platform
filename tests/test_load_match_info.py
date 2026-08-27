import json
from pathlib import Path
import sys


IMPECT_DIR = Path(__file__).resolve().parents[1] / "python" / "extract" / "impect"
sys.path.insert(0, str(IMPECT_DIR))

import load_match_info as loader  # noqa: E402


def test_payload_row_preserves_participation_arrays():
    payload = {
        "iterationId": 1410,
        "dateTime": "2026-01-01T15:00:00Z",
        "lastCalculationDate": "2026-01-01T18:00:00Z",
        "squadHome": {
            "id": 10,
            "players": [{"id": 1, "shirtNumber": 4}],
            "startingPositions": [{"playerId": 1, "position": "CENTRAL_DEFENDER"}],
            "substitutions": [{"playerId": 1, "toPosition": "BANK"}],
            "formations": [{"formation": "4-3-3"}],
        },
        "squadAway": {"id": 20},
    }

    row = loader.payload_row(payload, 99, None, 7)

    assert row["match_id"] == 99
    assert row["iteration_id"] == 1410
    assert row["home_squad_id"] == 10
    assert json.loads(row["home_starts"])[0]["playerId"] == 1
    assert json.loads(row["away_subs"]) == []
    assert json.loads(row["raw"])["squadHome"]["id"] == 10
    assert row["run_id"] == 7


def test_fetch_and_load_uses_unwrapped_payload(monkeypatch):
    payload = {"iterationId": 1410, "squadHome": {"id": 10}, "squadAway": {"id": 20}}
    saved = []
    monkeypatch.setattr(loader.api, "get_match_info", lambda match_id: {"data": payload})
    monkeypatch.setattr(loader, "upsert", saved.append)

    loader.fetch_and_load(99, 1400, 8)

    assert saved[0]["match_id"] == 99
    assert saved[0]["iteration_id"] == 1410
    assert saved[0]["run_id"] == 8


def test_backfill_continues_after_one_match_fails(monkeypatch, capsys):
    monkeypatch.setattr(loader, "missing_event_matches", lambda limit: [(1, 10), (2, 10)])
    loaded = []

    def fake_fetch(match_id, iteration_id, run_id=None):
        if match_id == 1:
            raise RuntimeError("temporary failure")
        loaded.append((match_id, iteration_id))

    monkeypatch.setattr(loader, "fetch_and_load", fake_fetch)

    result = loader.backfill(limit=None, pause_seconds=0, dry_run=False)

    assert result == {"candidates": 2, "loaded": 1, "failed": 1}
    assert loaded == [(2, 10)]
    assert "progress: 2/2 loaded=1 failed=1" in capsys.readouterr().out
