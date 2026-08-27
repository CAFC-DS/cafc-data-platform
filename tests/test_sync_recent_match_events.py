from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest


IMPECT_DIR = Path(__file__).resolve().parents[1] / "python" / "extract" / "impect"
sys.path.insert(0, str(IMPECT_DIR))

import sync_recent_match_events as sync  # noqa: E402


def _iteration(iteration_id=2601, competition_id=41, season="26/27"):
    return {
        "id": iteration_id,
        "season": season,
        "competition": {"id": competition_id, "name": "EFL Championship", "gender": "MALE"},
    }


def test_eligible_iterations_filters_competition_season_and_womens(monkeypatch):
    rows = [
        _iteration(),
        _iteration(2602, competition_id=42),
        _iteration(2603, season="25/26"),
        {
            **_iteration(2604),
            "competition": {"id": 41, "name": "Women's Championship", "gender": "FEMALE"},
        },
    ]
    monkeypatch.setattr(sync.api, "get_iterations", lambda: {"data": rows})

    selected = sync.eligible_iterations({"26/27"}, {41})

    assert list(selected) == [2601]


def test_scoped_dry_run_seeds_missing_and_keeps_deletions_in_scope(monkeypatch):
    past = "2026-08-20T19:45:00Z"
    future = "2099-08-20T19:45:00Z"
    catalog = [
        {"id": 1, "scheduledDate": past, "available": True},
        {"id": 2, "scheduledDate": future, "available": True},
        {"id": 3, "scheduledDate": future, "available": True},
    ]
    monkeypatch.setattr(sync.api, "get_iterations", lambda: {"data": [_iteration()]})
    monkeypatch.setattr(sync.api, "get_matches", lambda _iteration_id: {"data": catalog})
    monkeypatch.setattr(sync.api, "get_match_data_updates", lambda _since: {"data": []})
    monkeypatch.setattr(sync.api, "get_match_updates", lambda _since: {"data": []})
    monkeypatch.setattr(sync.api, "get_match_deletes", lambda _since: {"data": [{"id": 3}, {"id": 999}]})
    monkeypatch.setattr(sync, "load_known_state", lambda: {3: {"status": sync.SUCCESS}, 999: {"status": sync.SUCCESS}})
    monkeypatch.setattr(sync, "load_event_match_ids", lambda: set())
    monkeypatch.setattr(sync, "load_since", lambda default, feed_name: default)

    result = sync.run(
        bootstrap_days=14,
        overlap_minutes=15,
        seasons={"26/27"},
        match_lookup_pause_seconds=0,
        competition_ids={41},
        feed_name="MATCH_EVENTS_V5_COMPETITION_41",
        seed_missing=True,
        dry_run=True,
    )

    assert result == {"candidates": 1, "deletions": 1, "loaded": 0, "failed": 0}


def test_scoped_sync_replaces_delta_match_and_advances_own_cursor(monkeypatch):
    match = {
        "id": 1,
        "iterationId": 2601,
        "scheduledDate": "2026-08-20T19:45:00Z",
        "available": True,
    }
    monkeypatch.setattr(sync.api, "get_iterations", lambda: {"data": [_iteration()]})
    monkeypatch.setattr(sync.api, "get_matches", lambda _iteration_id: {"data": [match.copy()]})
    monkeypatch.setattr(sync.api, "get_match_data_updates", lambda _since: {"data": [{"id": 1, "date": "2026-08-21T00:00:00Z"}]})
    monkeypatch.setattr(sync.api, "get_match_updates", lambda _since: {"data": [match.copy()]})
    monkeypatch.setattr(sync.api, "get_match_deletes", lambda _since: {"data": []})
    monkeypatch.setattr(sync, "load_known_state", lambda: {1: {"status": sync.SUCCESS}})
    monkeypatch.setattr(sync, "load_event_match_ids", lambda: {1})
    monkeypatch.setattr(sync, "load_since", lambda default, feed_name: default)
    monkeypatch.setattr(sync, "_open_run", lambda: (object(), 77))
    monkeypatch.setattr(sync, "_close_run", lambda *args: None)
    monkeypatch.setattr(sync, "save_state", lambda *args: None)
    monkeypatch.setattr(sync.events, "fetch_events_for_match", lambda *args: [{"MATCH_ID": 1}])
    match_info_loaded = []
    monkeypatch.setattr(
        sync.match_info,
        "fetch_and_load",
        lambda match_id, iteration_id, run_id: match_info_loaded.append((match_id, iteration_id, run_id)),
    )
    replacements = []
    monkeypatch.setattr(
        sync.events,
        "load_events",
        lambda rows, replace_existing_match=False: replacements.append(replace_existing_match) or len(rows),
    )
    saved = []
    monkeypatch.setattr(sync, "save_since", lambda value, feed_name: saved.append((value, feed_name)))

    result = sync.run(
        bootstrap_days=14,
        overlap_minutes=15,
        seasons={"26/27"},
        match_lookup_pause_seconds=0,
        competition_ids={41},
        feed_name="MATCH_EVENTS_V5_COMPETITION_41",
        seed_missing=True,
    )

    assert result["loaded"] == 1
    assert replacements == [True]
    assert match_info_loaded == [(1, 2601, 77)]
    assert saved[0][1] == "MATCH_EVENTS_V5_COMPETITION_41"
    assert isinstance(saved[0][0], datetime)
    assert saved[0][0].tzinfo == timezone.utc


def test_no_scoped_iteration_is_an_error(monkeypatch):
    monkeypatch.setattr(sync.api, "get_iterations", lambda: {"data": [_iteration(competition_id=42)]})

    with pytest.raises(RuntimeError, match="No eligible IMPECT iterations"):
        sync.run(
            bootstrap_days=14,
            overlap_minutes=15,
            seasons={"26/27"},
            match_lookup_pause_seconds=0,
            competition_ids={41},
            feed_name="MATCH_EVENTS_V5_COMPETITION_41",
            seed_missing=True,
            dry_run=True,
        )
