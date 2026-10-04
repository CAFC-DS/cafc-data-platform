"""Offline tests for the batched IMPECT event backfill (no Snowflake, no IMPECT API)."""
from pathlib import Path
import sys

import pytest


IMPECT_DIR = Path(__file__).resolve().parents[1] / "python" / "extract" / "impect"
sys.path.insert(0, str(IMPECT_DIR))

import backfill_historical_match_events as bf  # noqa: E402
import load_match_events as ev  # noqa: E402
import load_match_info as info  # noqa: E402
import railway_backfill as rb  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((" ".join(sql.split()), params))
        for needle, error in self.conn.fail_on:
            if needle in sql:
                raise error
        self._rows = self.conn.script.get("SELECT DISTINCT MATCH_ID" if "SELECT DISTINCT MATCH_ID" in sql else "", [])

    def fetchall(self):
        return getattr(self, "_rows", [])

    def fetchone(self):
        return None


class FakeConn:
    def __init__(self, script=None, fail_on=()):
        self.statements = []
        self.script = script or {}
        self.fail_on = list(fail_on)
        self.closed = False
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def sql(self, needle):
        return [text for text, _ in self.statements if needle in text]


def _event(event_id, index):
    return {"id": event_id, "index": index, "gameTime": {"gameTime": "1:00", "gameTimeInSec": 60.0},
            "player": {"id": 5}, "pass": {"distance": 3}, "pxT": {"team": 0.1}}


def _rows(match_id, n=3, run_id=9):
    rows = []
    for index in range(n):
        row = ev._flatten_event(match_id, 1400, _event(index + 1, index), run_id)
        row["EVENT_KPIS"] = None
        rows.append(row)
    return rows


@pytest.fixture()
def staged(monkeypatch):
    calls = []

    def fake_write_pandas(conn, df, table_name, database, schema, overwrite, auto_create_table):
        calls.append({"table": table_name, "rows": len(df), "columns": list(df.columns)})
        return True, 1, len(df), []

    monkeypatch.setattr(ev, "write_pandas", fake_write_pandas)
    monkeypatch.setattr(ev.config, "SNOWFLAKE_DATABASE", "CAFC_DB")
    monkeypatch.setattr(ev.config, "SNOWFLAKE_SCHEMA", "IMPECT_RAW")
    return calls


def test_batch_load_uses_one_insert_and_no_updates(staged):
    conn = FakeConn()
    rows = _rows(101) + _rows(102)

    inserted = ev.load_events_batch(rows, conn=conn)

    assert inserted == 6
    assert staged == [{"table": "EVENTS_LOAD_STAGE", "rows": 6, "columns": list(rows[0].keys())}]
    inserts = conn.sql("INSERT INTO CAFC_DB.IMPECT_RAW.EVENTS")
    assert len(inserts) == 1
    assert inserts[0].count("PARSE_JSON(") == len(ev._VARIANT_COLUMNS) == 12
    assert not conn.sql("UPDATE ")  # the old design ran one UPDATE per VARIANT column
    assert not conn.sql("DELETE")  # nothing to replace, so no DELETE at all
    assert [t for t, _ in conn.statements if t in ("BEGIN", "COMMIT")] == ["BEGIN", "COMMIT"]
    assert "VARCHAR AS RAW_EVENT" in conn.sql("CREATE OR REPLACE TEMPORARY TABLE")[0]


def test_batch_load_replaces_only_matches_that_already_have_rows(staged):
    conn = FakeConn(script={"SELECT DISTINCT MATCH_ID": [(102,)]})

    ev.load_events_batch(_rows(101) + _rows(102), replace_existing=True, conn=conn)

    statements = [text for text, _ in conn.statements]
    deletes = conn.sql("DELETE FROM")
    assert len(deletes) == 1 and "MATCH_ID IN (102)" in deletes[0]
    # existence check first, old rows removed only after the new ones are in, inside one transaction
    assert statements.index(conn.sql("SELECT DISTINCT MATCH_ID")[0]) < statements.index("BEGIN")
    assert statements.index("BEGIN") < statements.index(conn.sql("INSERT INTO")[0]) \
        < statements.index(deletes[0]) < statements.index("COMMIT")
    delete_params = [params for text, params in conn.statements if text.startswith("DELETE")][0]
    assert delete_params == {"run_id": 9}


def test_batch_load_rolls_back_and_raises_when_insert_fails(staged):
    conn = FakeConn(fail_on=[("INSERT INTO", RuntimeError("boom"))])

    with pytest.raises(RuntimeError, match="boom"):
        ev.load_events_batch(_rows(101), conn=conn)

    assert "ROLLBACK" in [text for text, _ in conn.statements]
    assert "COMMIT" not in [text for text, _ in conn.statements]


def test_batch_load_does_not_close_a_caller_owned_connection(staged):
    conn = FakeConn()
    ev.load_events_batch(_rows(101), conn=conn)
    assert conn.closed is False


def test_load_events_wrapper_keeps_old_signature(monkeypatch):
    seen = {}
    monkeypatch.setattr(ev, "load_events_batch",
                        lambda rows, replace_existing=False, conn=None: seen.update(rows=rows, replace=replace_existing) or 3)
    assert ev.load_events([{"MATCH_ID": 1}], replace_existing_match=True) == 3
    assert seen == {"rows": [{"MATCH_ID": 1}], "replace": True}


def test_match_info_upsert_many_is_one_merge(monkeypatch):
    calls = []
    monkeypatch.setattr(info, "write_pandas",
                        lambda conn, df, table_name, database, schema, overwrite, auto_create_table:
                        calls.append((table_name, len(df), list(df.columns))) or (True, 1, len(df), []))
    conn = FakeConn()
    rows = [info.payload_row({"squadHome": {"id": 1}, "squadAway": {"id": 2}}, mid, 1400, 9) for mid in (11, 12, 13)]

    assert info.upsert_many(rows, conn=conn) == 3

    assert calls == [("MATCH_INFO_LOAD_STAGE", 3, list(info._STAGE_COLUMNS))]
    assert len(conn.sql("MERGE INTO CAFC_DB.IMPECT_RAW.MATCH_INFO")) == 1
    assert conn.closed is False


def test_finish_matches_is_a_single_update():
    conn = FakeConn()
    outcomes = {
        3: {"status": "SUCCESS", "count": 2700, "error": None},
        1: {"status": "NO_EVENT_DATA", "count": 0, "error": None},
        2: {"status": "FAILED", "count": None, "error": "x" * 5000},
    }

    bf._finish_matches(conn, 77, outcomes)

    assert len(conn.statements) == 1
    sql, params = conn.statements[0]
    assert sql.startswith(f"UPDATE {bf.QUEUE_TABLE} t")
    assert sql.count("%(m") == 3
    assert params["run_id"] == 77
    assert [params[f"m{i}"] for i in range(3)] == [1, 2, 3]          # sorted by match id
    assert len(params["e1"]) == 4000                                  # error truncated
    assert conn.commits == 1


def test_split_by_rows_keeps_order_and_respects_the_cap():
    fetched = [{"rows": [0] * n, "id": i} for i, n in enumerate([40, 40, 40, 100, 10])]

    groups = bf._split_by_rows(fetched, 90)

    assert [[r["id"] for r in g] for g in groups] == [[0, 1], [2], [3], [4]]


def _patch_pipeline(monkeypatch, conn, claimed, fetch):
    monkeypatch.setattr(bf, "get_connection", lambda: conn)
    monkeypatch.setattr(bf, "_claim_batch", lambda *a, **k: ("token-1", claimed))
    monkeypatch.setattr(bf.events, "_open_run", lambda cur, who: 7)
    monkeypatch.setattr(bf.events, "_close_run", lambda cur, run_id, status, notes="": conn.statements.append((f"CLOSE {status}", notes)))
    monkeypatch.setattr(bf.events, "fetch_events_for_match", fetch)
    monkeypatch.setattr(bf.match_info, "fetch_row", lambda mid, iid, run_id: {"match_id": mid})


def _items(*ids):
    return [{"match_id": i, "iteration_id": 1400, "competition_name": "c", "season": "25/26"} for i in ids]


def test_process_loads_a_batch_with_one_load_and_one_queue_update(monkeypatch):
    conn = FakeConn()

    def fetch(match_id, iteration_id, run_id):
        return [] if match_id == 3 else _rows(match_id, run_id=run_id)

    _patch_pipeline(monkeypatch, conn, _items(1, 2, 3), fetch)
    loads, merges = [], []
    monkeypatch.setattr(bf.events, "load_events_batch",
                        lambda rows, replace_existing, conn: loads.append((len(rows), replace_existing)) or len(rows))
    monkeypatch.setattr(bf.match_info, "upsert_many", lambda rows, conn: merges.append(len(rows)) or len(rows))

    result = bf.process(25, 5, {1400}, load_matches=10, fetch_workers=2)

    assert result == {"claimed": 3, "success": 2, "no_event_data": 1, "failed": 0, "rows": 6}
    assert loads == [(6, True)]          # both loadable matches in ONE events load
    assert merges == [2]                 # and ONE match-info merge
    assert len(conn.sql(f"UPDATE {bf.QUEUE_TABLE} t")) == 1
    assert ("CLOSE SUCCESS", "claimed=3 success=2 no_event_data=1 failed=0") in conn.statements
    assert conn.closed is True


def test_process_isolates_a_bad_match_instead_of_failing_the_group(monkeypatch):
    conn = FakeConn()
    _patch_pipeline(monkeypatch, conn, _items(1, 2, 3), lambda mid, iid, run_id: _rows(mid, run_id=run_id))

    def load(rows, replace_existing, conn):
        if len({r["MATCH_ID"] for r in rows}) > 1 or rows[0]["MATCH_ID"] == 2:
            raise RuntimeError("bad rows")
        return len(rows)

    monkeypatch.setattr(bf.events, "load_events_batch", load)
    monkeypatch.setattr(bf.match_info, "upsert_many", lambda rows, conn: len(rows))

    result = bf.process(25, 5, None, load_matches=10, fetch_workers=1)

    assert (result["success"], result["failed"]) == (2, 1)
    _, params = [s for s in conn.statements if s[0].startswith(f"UPDATE {bf.QUEUE_TABLE} t")][0]
    statuses = {params[f"m{i}"]: params[f"s{i}"] for i in range(3)}
    assert statuses == {1: "SUCCESS", 2: "FAILED", 3: "SUCCESS"}
    assert "bad rows" in params["e1"]


def test_process_marks_fetch_errors_failed_without_loading_them(monkeypatch):
    conn = FakeConn()

    def fetch(match_id, iteration_id, run_id):
        raise RuntimeError("404 error from /events")

    _patch_pipeline(monkeypatch, conn, _items(1), fetch)
    monkeypatch.setattr(bf.events, "load_events_batch", lambda *a, **k: pytest.fail("must not load"))

    result = bf.process(25, 5, None, fetch_workers=1)

    assert result["failed"] == 1 and result["success"] == 0


def test_process_hands_claims_back_when_interrupted(monkeypatch):
    conn = FakeConn()
    _patch_pipeline(monkeypatch, conn, _items(1, 2), lambda mid, iid, run_id: _rows(mid, run_id=run_id))

    def load(rows, replace_existing, conn):
        raise SystemExit(143)  # what the SIGTERM handler raises

    monkeypatch.setattr(bf.events, "load_events_batch", load)

    with pytest.raises(SystemExit):
        bf.process(25, 5, None, fetch_workers=1)

    release = conn.sql("ATTEMPT_COUNT=GREATEST(ATTEMPT_COUNT - 1, 0)")
    assert len(release) == 1                                  # unfinished claims returned, attempt not burnt
    _, params = [s for s in conn.statements if "GREATEST(ATTEMPT_COUNT - 1" in s[0]][0]
    assert params == {"claim": "token-1"}
    assert ("CLOSE FAILED", "backfill batch interrupted") in conn.statements


def test_process_reports_when_nothing_is_claimable(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(bf, "get_connection", lambda: conn)
    monkeypatch.setattr(bf, "_claim_batch", lambda *a, **k: ("t", []))

    assert bf.process(25, 5, None) == {"claimed": 0, "success": 0, "no_event_data": 0, "failed": 0, "rows": 0}


# ---------------------------------------------------------------- supervisor


class FakeProc:
    started = []

    def __init__(self, command):
        FakeProc.started.append(command)

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def _counts(remaining=0, done=0, deferred=0, exhausted=0):
    return {"remaining": remaining, "pending": remaining, "running": 0, "retryable": 0,
            "deferred": deferred, "exhausted": exhausted, "done": done}


@pytest.fixture()
def supervisor(monkeypatch):
    FakeProc.started = []
    for name in rb.REQUIRED_ENV:
        monkeypatch.setenv(name, "x")
    for name in ("BACKFILL_ITERATION_IDS", "BACKFILL_SEASONS", "BACKFILL_MAX_MATCHES", "BACKFILL_MAX_MINUTES",
                 "BACKFILL_MAX_STALLED_MINUTES", "BACKFILL_WORKERS", "BACKFILL_PLAN", "BACKFILL_BLOCKS",
                 "BACKFILL_DRY_RUN", "BACKFILL_QUERY_TAG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKFILL_ITERATION_IDS", "10,20")
    monkeypatch.setattr(rb, "release_stale_claims", lambda ids, minutes: 0)
    monkeypatch.setattr(rb.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(rb.time, "sleep", lambda s: None)
    monkeypatch.setattr(rb.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(rb, "_prefetch_token", lambda: None)

    def script(sequence):
        iterator = iter(sequence)
        monkeypatch.setattr(rb, "scope_counts", lambda ids, attempts: next(iterator))

    return script


def test_supervisor_exits_zero_once_the_scope_is_drained(supervisor, monkeypatch):
    monkeypatch.setenv("BACKFILL_WORKERS", "2")
    supervisor([_counts(5, 0), _counts(0, 5), _counts(0, 5)])

    assert rb.supervise() == 0
    assert len(FakeProc.started) == 2
    assert "--iteration-ids" in FakeProc.started[0] and "10,20" in FakeProc.started[0]


def test_supervisor_does_not_wait_for_cooldown_or_exhausted_rows(supervisor):
    supervisor([_counts(0, 40, deferred=1, exhausted=2)])

    assert rb.supervise() == 0
    assert FakeProc.started == []        # nothing claimable, so no workers are even started


def test_supervisor_gives_up_when_it_cannot_progress(supervisor, monkeypatch):
    monkeypatch.setenv("BACKFILL_MAX_STALLED_MINUTES", "0")
    supervisor([_counts(3, 1), _counts(3, 1)])

    assert rb.supervise() == 1


def test_supervisor_stops_at_the_match_budget(supervisor, monkeypatch):
    monkeypatch.setenv("BACKFILL_MAX_MATCHES", "100")
    monkeypatch.setenv("BACKFILL_WORKERS", "4")
    supervisor([_counts(500, 0), _counts(400, 100), _counts(400, 100)])

    assert rb.supervise() == 0
    command = FakeProc.started[0]
    assert command[command.index("--max-matches") + 1] == "25"   # 100 split across 4 workers
    assert len(FakeProc.started) == 4                            # one round only


def test_supervisor_refuses_to_start_without_scope_or_credentials(supervisor, monkeypatch):
    monkeypatch.delenv("BACKFILL_ITERATION_IDS")
    assert rb.supervise() == 1

    monkeypatch.setenv("BACKFILL_ITERATION_IDS", "10")
    monkeypatch.delenv("IMPECT_PASSWORD")
    assert rb.supervise() == 1


def test_scope_resolution_unions_ids_and_seasons(monkeypatch):
    monkeypatch.setenv("BACKFILL_ITERATION_IDS", "30, 10")
    monkeypatch.setenv("BACKFILL_SEASONS", "25/26, 2025")
    seen = {}

    class Cursor:
        def execute(self, sql, params):
            seen["params"] = params

        def fetchall(self):
            return [(20,), (10,)]

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(rb, "get_connection", lambda: Conn())

    assert rb.resolve_scope() == [10, 20, 30]
    assert seen["params"] == {"s0": "25/26", "s1": "2025"} and seen["closed"]
