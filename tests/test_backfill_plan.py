"""Offline tests: staged backfill plan, staged supervisor and the IMPECT token fix."""
import json
from pathlib import Path
import sys
import threading

import pytest


IMPECT_DIR = Path(__file__).resolve().parents[1] / "python" / "extract" / "impect"
sys.path.insert(0, str(IMPECT_DIR))

import backfill_plan as plan_mod  # noqa: E402
import impect_api as api  # noqa: E402
import railway_backfill as rb  # noqa: E402


def _row(iteration_id, season, country_id, matches=10, todo=10):
    return {"iteration_id": iteration_id, "season": season, "country_id": country_id,
            "matches": matches, "todo": todo}


# ------------------------------------------------------------------- the plan


def test_plan_orders_blocks_newest_first_and_groups_by_region():
    rows = [
        _row(1, "25/26", 549), _row(2, "2026", 544), _row(3, "26/27", 393),
        _row(4, "26/27", 462), _row(5, "2025", 412), _row(6, "22/23", 466),
    ]

    plan = plan_mod.build_plan(rows, generated_at="t")

    assert [b["label"] for b in plan["blocks"]] == ["26/27+2026", "25/26+2025", "24/25+2024", "23/24+2023", "22/23+2022"]
    first = plan["blocks"][0]["stages"]
    assert [(s["group"], s["iteration_ids"]) for s in first] == [
        ("Nordics & Baltics", [3, 4]),            # Norway + Sweden share a stage
        ("North & Central America", [2]),         # USA, calendar-year 2026 sits in the same block
    ]
    assert [(s["group"], s["iteration_ids"]) for s in plan["blocks"][1]["stages"]] == [
        ("British Isles", [1]), ("South America", [5])]
    assert plan["blocks"][2]["stages"] == []      # nothing in 24/25+2024 in this sample
    assert plan["blocks"][4]["stages"][0]["group"] == "Germany, Austria & Switzerland"


def test_plan_ignores_other_seasons_and_keeps_unmapped_countries_visible():
    plan = plan_mod.build_plan([_row(1, "21/22", 549), _row(2, "26/27", 99999), _row(3, "26/27", None)])

    stages = plan["blocks"][0]["stages"]
    assert [s["group"] for s in stages] == [plan_mod.OTHER_GROUP]
    assert stages[0]["iteration_ids"] == [2, 3]
    assert all(not b["stages"] for b in plan["blocks"][1:])


def test_plan_sums_matches_and_todo_per_stage():
    plan = plan_mod.build_plan([_row(1, "26/27", 393, 100, 40), _row(2, "2026", 462, 50, 5)])

    stage = plan["blocks"][0]["stages"][0]
    assert (stage["matches"], stage["todo"]) == (150, 45)


def test_every_country_in_the_groups_appears_once():
    ids = [cid for _, group in plan_mod.REGION_GROUPS for cid in group]
    assert len(ids) == len(set(ids))


def test_select_stages_filters_blocks_and_rejects_typos():
    plan = plan_mod.build_plan([_row(1, "26/27", 393), _row(2, "25/26", 549), _row(3, "2025", 412)])

    assert [s["block"] for s in plan_mod.select_stages(plan)] == ["26/27+2026", "25/26+2025", "25/26+2025"]
    only = plan_mod.select_stages(plan, ["25/26+2025"])
    assert [(s["block"], s["group"]) for s in only] == [("25/26+2025", "British Isles"), ("25/26+2025", "South America")]
    with pytest.raises(ValueError, match="unknown plan block"):
        plan_mod.select_stages(plan, ["26-27"])


def test_summary_mentions_every_block_and_the_total():
    text = plan_mod.format_summary(plan_mod.build_plan([_row(1, "26/27", 393, 100, 40)], generated_at="t"))
    assert "26/27+2026: 40 to do of 100" in text and "total: 40 to do of 100" in text


# ------------------------------------------------------ staged supervisor run


class FakeProc:
    started = []

    def __init__(self, command):
        FakeProc.started.append(command)

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def _counts(remaining=0, done=0):
    return {"remaining": remaining, "pending": remaining, "running": 0, "retryable": 0,
            "deferred": 0, "exhausted": 0, "done": done}


@pytest.fixture()
def staged(monkeypatch, tmp_path):
    FakeProc.started = []
    for name in rb.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    for name in ("BACKFILL_ITERATION_IDS", "BACKFILL_SEASONS", "BACKFILL_MAX_MATCHES", "BACKFILL_MAX_MINUTES",
                 "BACKFILL_MAX_STALLED_MINUTES", "BACKFILL_WORKERS", "BACKFILL_BLOCKS", "BACKFILL_DRY_RUN",
                 "BACKFILL_QUERY_TAG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKFILL_WORKERS", "2")
    plan = plan_mod.build_plan([_row(10, "26/27", 393), _row(20, "26/27", 549), _row(30, "25/26", 412)], "t")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    monkeypatch.setenv("BACKFILL_PLAN", str(path))
    monkeypatch.setattr(rb, "release_stale_claims", lambda ids, minutes: 0)
    monkeypatch.setattr(rb, "_prefetch_token", lambda: None)
    monkeypatch.setattr(rb.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(rb.time, "sleep", lambda s: None)
    monkeypatch.setattr(rb.signal, "signal", lambda *a, **k: None)

    def script(by_ids):
        """by_ids: iteration-id tuple -> list of count dicts handed out in order."""
        queues = {key: list(value) for key, value in by_ids.items()}
        monkeypatch.setattr(rb, "scope_counts", lambda ids, attempts: queues[tuple(ids)].pop(0))

    return script


def test_plan_runs_each_stage_in_order_with_its_own_scope_and_tag(staged):
    staged({(10,): [_counts(5, 0), _counts(0, 5), _counts(0, 5)],
            (20,): [_counts(3, 0), _counts(0, 3), _counts(0, 3)],
            (30,): [_counts(2, 0), _counts(0, 2), _counts(0, 2)]})

    assert rb.supervise() == 0

    ids = [cmd[cmd.index("--iteration-ids") + 1] for cmd in FakeProc.started[::2]]
    tags = [cmd[cmd.index("--query-tag") + 1] for cmd in FakeProc.started[::2]]
    assert ids == ["10", "20", "30"]
    assert tags[0].endswith("block=26/27+2026;group=Nordics & Baltics")
    assert tags[1].endswith("block=26/27+2026;group=British Isles")
    assert tags[2].endswith("block=25/26+2025;group=South America")
    assert len(FakeProc.started) == 6                      # 2 workers x 3 stages


def test_a_finished_stage_is_skipped_without_starting_workers(staged):
    staged({(10,): [_counts(0, 9)], (20,): [_counts(3, 0), _counts(0, 3), _counts(0, 3)], (30,): [_counts(0, 4)]})

    assert rb.supervise() == 0
    assert len(FakeProc.started) == 2 and "20" in FakeProc.started[0]


def test_blocks_variable_limits_the_run_to_those_blocks(staged, monkeypatch):
    monkeypatch.setenv("BACKFILL_BLOCKS", "25/26+2025")
    staged({(30,): [_counts(2, 0), _counts(0, 2), _counts(0, 2)]})

    assert rb.supervise() == 0
    assert len(FakeProc.started) == 2 and "30" in FakeProc.started[0]


def test_a_stalled_stage_does_not_stop_later_stages_but_fails_the_run(staged, monkeypatch):
    monkeypatch.setenv("BACKFILL_MAX_STALLED_MINUTES", "0")
    staged({(10,): [_counts(3, 1), _counts(3, 1)],
            (20,): [_counts(3, 0), _counts(0, 3), _counts(0, 3)],
            (30,): [_counts(0, 4)]})

    assert rb.supervise() == 1
    assert [cmd[cmd.index("--iteration-ids") + 1] for cmd in FakeProc.started[::2]] == ["10", "20"]


def test_the_match_budget_spans_stages(staged, monkeypatch):
    monkeypatch.setenv("BACKFILL_MAX_MATCHES", "8")
    staged({(10,): [_counts(5, 0), _counts(0, 5), _counts(0, 5)],
            (20,): [_counts(5, 0), _counts(2, 3), _counts(2, 3)]})

    assert rb.supervise() == 0
    ids = [cmd[cmd.index("--iteration-ids") + 1] for cmd in FakeProc.started[::2]]
    assert ids == ["10", "20"]                              # budget hit inside stage 2, stage 3 never starts
    second = FakeProc.started[2]
    assert second[second.index("--max-matches") + 1] == "2"   # (8 - 5 done) / 2 workers, rounded up


def test_dry_run_reports_every_stage_and_starts_nothing(staged, monkeypatch, capsys):
    monkeypatch.setenv("BACKFILL_DRY_RUN", "1")
    staged({(10,): [_counts(5, 0)], (20,): [_counts(3, 0)], (30,): [_counts(2, 0)]})

    assert rb.supervise() == 0
    out = capsys.readouterr().out
    assert out.count("stage ") == 3 and "dry run" in out
    assert FakeProc.started == []


def test_unknown_block_or_missing_plan_file_is_a_clean_failure(staged, monkeypatch):
    monkeypatch.setenv("BACKFILL_BLOCKS", "typo")
    assert rb.supervise() == 1
    monkeypatch.delenv("BACKFILL_BLOCKS")
    monkeypatch.setenv("BACKFILL_PLAN", "/nonexistent/plan.json")
    assert rb.supervise() == 1


# ------------------------------------------------------------- IMPECT token


@pytest.fixture()
def token_env(monkeypatch):
    store = {}
    monkeypatch.setattr(api.config, "IMPECT_USERNAME", "user")
    monkeypatch.setattr(api.config, "IMPECT_PASSWORD", "pass")
    monkeypatch.setattr(api, "load_cached_token", lambda: store.get("token"))
    monkeypatch.setattr(api, "save_token_cache", lambda token, expires_in: store.update(token={"token": token}))
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    return store


class _Response:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise api.requests.exceptions.HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return {"access_token": "tok-1", "expires_in": 3600}


def test_concurrent_callers_share_one_login(token_env, monkeypatch):
    posts = []
    barrier = threading.Barrier(6)

    def post(*args, **kwargs):
        posts.append(1)
        return _Response()

    monkeypatch.setattr(api.requests, "post", post)
    results = []

    def worker():
        barrier.wait()
        results.append(api.get_auth_token())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["tok-1"] * 6
    assert len(posts) == 1


def test_a_refused_login_is_retried_before_failing(token_env, monkeypatch):
    answers = [_Response(401), _Response(401), _Response(200)]
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: answers.pop(0))

    assert api.get_auth_token() == "tok-1"
    assert answers == []


def test_a_login_that_keeps_failing_raises_after_the_retries(token_env, monkeypatch):
    calls = []
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: calls.append(1) or _Response(401))

    with pytest.raises(Exception, match="Failed to get authentication token"):
        api.get_auth_token()
    assert len(calls) == api.TOKEN_REQUEST_ATTEMPTS


def test_token_cache_is_written_atomically(tmp_path, monkeypatch):
    target = tmp_path / "cache.json"
    monkeypatch.setattr(api.config, "TOKEN_CACHE_FILE", str(target))

    api.save_token_cache("abc", 3600)

    assert json.loads(target.read_text())["token"] == "abc"
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]   # no stray temp file left behind


# ------------------------------------------------------------- queue refresh

import refresh_backfill_queue as rq  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def test_queue_refresh_skips_future_matches_and_counts_new_ones():
    now = datetime(2026, 10, 4, tzinfo=timezone.utc)
    iteration = {"id": 1864, "season": "2026", "competition": {"name": "Major League Soccer"}}
    matches = [
        {"id": 1, "scheduledDate": "2026-03-01T20:00:00Z"},
        {"id": 2, "scheduledDate": "2026-09-01T20:00:00Z"},
        {"id": 3, "scheduledDate": "2026-11-01T20:00:00Z"},   # not played yet
    ]

    rows, counts = rq.rows_for_iteration(iteration, matches, loaded={1}, queued={1}, now=now)

    assert counts == {"api": 3, "completed": 2, "new": 1, "future": 1}
    assert [(r["MATCH_ID"], r["ALREADY_LOADED"]) for r in rows] == [(1, True), (2, False)]
    assert rows[0]["COMPETITION_NAME"] == "Major League Soccer" and rows[0]["SEASON"] == "2026"


def test_missing_countries_are_filled_from_the_api_iteration_list():
    rows = [_row(1, "26/27", None), _row(2, "26/27", 393), _row(3, "26/27", None)]
    api_iterations = [{"id": 1, "competition": {"countryId": 462}}, {"id": 2, "competition": {"countryId": 1}},
                      {"id": 3, "competition": {}}]

    filled = plan_mod.fill_missing_countries(rows, api_iterations)

    assert [r["country_id"] for r in filled] == [462, 393, None]   # known ids untouched, unknown stay unmapped
