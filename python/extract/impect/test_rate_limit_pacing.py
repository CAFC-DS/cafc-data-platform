"""Unit tests for the adaptive cross-call rate-limit pacing in impect_api.

Run:  python3 -m pytest test_rate_limit_pacing.py -q
No network or credentials required -- the HTTP session and time.sleep are
stubbed.
"""
from __future__ import annotations

import pytest

import impect_api as api


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {"data": []}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise api.requests.exceptions.HTTPError(f"{self.status_code}")


class _FakeSession:
    """Returns scripted responses in order; repeats the last one forever."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


@pytest.fixture(autouse=True)
def _reset_and_stub(monkeypatch):
    # Fresh pace state per test.
    monkeypatch.setattr(api, "_pace_delay", 0.0, raising=False)
    monkeypatch.setattr(api, "PACE_FLOOR_SECONDS", 0.0, raising=False)
    # No real auth, no real sleeping.
    monkeypatch.setattr(api, "get_auth_headers", lambda: {})
    slept: list[float] = []
    monkeypatch.setattr(api.time, "sleep", lambda s: slept.append(s))
    return slept


def _run(monkeypatch, responses):
    session = _FakeSession(responses)
    monkeypatch.setattr(api, "get_http_session", lambda: session)
    result = api.make_request("/v5/customerapi/thing")
    return result, session


def test_clean_response_keeps_pace_at_zero(monkeypatch, _reset_and_stub):
    _run(monkeypatch, [_FakeResponse(200, payload={"data": [1]})])
    assert api._current_pace() == 0.0
    # No pre-request pacing sleep was needed.
    assert _reset_and_stub == []


def test_retry_after_header_is_honoured(monkeypatch, _reset_and_stub):
    _run(monkeypatch, [
        _FakeResponse(429, headers={"Retry-After": "7"}),
        _FakeResponse(200, payload={"data": ["ok"]}),
    ])
    # The 7s the server asked for was slept at least once.
    assert 7.0 in _reset_and_stub
    # And the process-wide pace was nudged up off zero.
    assert api._current_pace() > 0.0


def test_retry_after_is_capped(monkeypatch, _reset_and_stub):
    _run(monkeypatch, [
        _FakeResponse(429, headers={"Retry-After": "999999"}),
        _FakeResponse(200),
    ])
    assert max(_reset_and_stub) <= api.MAX_RETRY_AFTER_SECONDS


def test_pace_grows_on_429_then_decays_on_success(monkeypatch, _reset_and_stub):
    _run(monkeypatch, [_FakeResponse(429), _FakeResponse(200)])
    bumped = api._pace_delay
    assert bumped > 0.0

    # A run of clean calls should walk the pace back down toward zero.
    monkeypatch.setattr(api, "get_http_session",
                        lambda: _FakeSession([_FakeResponse(200)]))
    for _ in range(40):
        api.make_request("/v5/customerapi/thing")
    assert api._pace_delay < bumped
    assert api._pace_delay < 0.05


def test_existing_pace_is_slept_before_the_request(monkeypatch, _reset_and_stub):
    monkeypatch.setattr(api, "_pace_delay", 3.0, raising=False)
    _run(monkeypatch, [_FakeResponse(200)])
    assert 3.0 in _reset_and_stub


def test_pace_never_exceeds_its_ceiling(monkeypatch, _reset_and_stub):
    # 9 consecutive 429s (attempt budget is 10) then give up.
    session = _FakeSession([_FakeResponse(429)])
    monkeypatch.setattr(api, "get_http_session", lambda: session)
    with pytest.raises(Exception):
        api.make_request("/v5/customerapi/thing")
    assert api._pace_delay <= api.PACE_MAX_SECONDS


def test_environment_floor_applies(monkeypatch, _reset_and_stub):
    monkeypatch.setattr(api, "PACE_FLOOR_SECONDS", 1.5, raising=False)
    assert api._current_pace() == 1.5
    _run(monkeypatch, [_FakeResponse(200)])
    # Floor is slept even though the adaptive delay is still zero.
    assert 1.5 in _reset_and_stub


def test_unparseable_retry_after_falls_back_to_backoff(monkeypatch, _reset_and_stub):
    assert api._parse_retry_after("not-a-date") is None
    _run(monkeypatch, [
        _FakeResponse(429, headers={"Retry-After": "garbage"}),
        _FakeResponse(200),
    ])
    # First 429 fell back to the 1s exponential-backoff seed.
    assert 1.0 in _reset_and_stub
