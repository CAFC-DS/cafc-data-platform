from pathlib import Path
import sys

import pytest

IMPECT_DIR = Path(__file__).resolve().parents[1] / "python" / "extract" / "impect"
sys.path.insert(0, str(IMPECT_DIR))

import sync_championship_set_pieces as sync  # noqa: E402


def test_run_resolves_current_iterations_and_scopes_backfill(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "eligible_iterations", lambda seasons, comp_ids: {2114: {"season": "26/27"}})
    monkeypatch.setattr(sync.load_set_pieces, "backfill",
                         lambda **kwargs: captured.update(kwargs) or {"candidates": 79, "loaded": 79, "failed": 0})

    result = sync.run(seasons={"26/27", "2026", "2027"})

    assert result == {"candidates": 79, "loaded": 79, "failed": 0}
    assert captured["iteration_ids"] == {2114}
    assert captured["dry_run"] is False


def test_run_raises_when_no_eligible_iterations(monkeypatch):
    monkeypatch.setattr(sync, "eligible_iterations", lambda seasons, comp_ids: {})

    with pytest.raises(RuntimeError, match="No eligible IMPECT Championship iterations"):
        sync.run(seasons={"26/27"})
