from datetime import datetime

from python.identity.fixture_matcher import fetch_reversed, update_reversed


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.executed = []
        self.many = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def executemany(self, sql, params):
        self.many.append((sql, list(params)))

    def fetchall(self):
        return self._rows


def test_fetch_reversed_only_targets_primary_impect_created_fixtures():
    cur = FakeCursor([(1, 2, 3, 4, 5, datetime(2026, 9, 2))])

    assert fetch_reversed(cur) == cur._rows
    sql = cur.executed[0]
    assert "CREATED_FROM_SOURCE = 'IMPECT'" in sql
    assert "SOURCE_SYSTEM = 'IMPECT'" in sql
    assert "fi.IS_PRIMARY" in sql
    assert "f.HOME_SQUAD_ID = m.AWAYSQUADID" in sql
    assert "FIXTURE_DATE" not in sql


def test_update_reversed_swaps_home_and_away_on_fixture_and_identity():
    sched = datetime(2026, 9, 2, 18, 45)
    # (fixture_id, identity_id, match_id, home, away, scheduled_at)
    cur = FakeCursor()

    n = update_reversed(cur, [(2864025, 77, 270221, 877, 956, sched)])

    assert n == 1
    (fx_sql, fx_params), (id_sql, id_params) = cur.many
    assert "CORE.FIXTURES" in fx_sql
    assert fx_params == [{"fid": 2864025, "home": 877, "away": 956}]
    assert "FIXTURE_IDENTITIES" in id_sql
    assert id_params == [{"iid": 77, "home": "877", "away": "956"}]


def test_update_reversed_noop_when_nothing_reversed():
    cur = FakeCursor()
    assert update_reversed(cur, []) == 0
    assert cur.many == []
