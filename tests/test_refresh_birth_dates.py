import pytest

from python.identity.refresh_birth_dates import refresh_birth_dates


class FakeCursor:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._result


def test_dry_run_only_counts():
    cur = FakeCursor((7,))
    assert refresh_birth_dates(cur, "IMPECT", dry_run=True) == 7
    sql, params = cur.calls[0]
    assert "SELECT COUNT(*)" in sql
    assert "MERGE" not in sql
    assert params == {"src": "IMPECT"}


def test_apply_merges_and_returns_updated_rows():
    cur = FakeCursor((3,))
    assert refresh_birth_dates(cur, "impect ", dry_run=False) == 3
    sql, params = cur.calls[0]
    assert "MERGE INTO CAFC_DB.CORE.PLAYERS" in sql
    assert params == {"src": "IMPECT"}


def test_rule_guards_are_in_sql():
    cur = FakeCursor((0,))
    refresh_birth_dates(cur, "IMPECT", dry_run=False)
    sql = cur.calls[0][0]
    # latest season wins
    assert "ORDER BY ITERATION_ID DESC" in sql
    # never blank a DOB, never touch retired players
    assert "l.birth_date IS NOT NULL" in sql
    assert "COALESCE(p.IS_ACTIVE, TRUE)" in sql
    # hand-entered (non-provider) DOBs are only filled when missing
    assert "p.CREATED_FROM_SOURCE = %(src)s OR p.BIRTH_DATE IS NULL" in sql


def test_unsupported_source_raises():
    with pytest.raises(NotImplementedError):
        refresh_birth_dates(FakeCursor((0,)), "WYSCOUT", dry_run=True)
