from pathlib import Path
import sys

IMPECT_DIR = Path(__file__).resolve().parents[1] / "python" / "extract" / "impect"
sys.path.insert(0, str(IMPECT_DIR))

import impect_api as api  # noqa: E402


def test_get_match_set_pieces_calls_expected_path(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "make_request", lambda endpoint, params=None: calls.append((endpoint, params)) or {"data": []})

    result = api.get_match_set_pieces(267839)

    assert calls == [("/v5/customerapi/matches/267839/set-pieces", None)]
    assert result == {"data": []}
