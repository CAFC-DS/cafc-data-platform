from datetime import datetime

from python.identity.fixture_matcher import (
    count_reid_blocked,
    fetch_reid,
    fetch_reversed,
    update_squads,
)


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


def test_update_squads_swaps_home_and_away_on_fixture_and_identity():
    sched = datetime(2026, 9, 2, 18, 45)
    # (fixture_id, identity_id, match_id, home, away, scheduled_at)
    cur = FakeCursor()

    n = update_squads(cur, [(2864025, 77, 270221, 877, 956, sched)])

    assert n == 1
    (fx_sql, fx_params), (id_sql, id_params) = cur.many
    assert "CORE.FIXTURES" in fx_sql
    assert fx_params == [{"fid": 2864025, "home": 877, "away": 956}]
    assert "FIXTURE_IDENTITIES" in id_sql
    assert id_params == [{"iid": 77, "home": "877", "away": "956"}]


def test_update_squads_noop_when_nothing_reversed():
    cur = FakeCursor()
    assert update_squads(cur, []) == 0
    assert cur.many == []


def test_fetch_reid_requires_both_new_squads_to_resolve_as_male():
    cur = FakeCursor([(9, 8, 7, 749, 768, datetime(2026, 9, 2))])

    assert fetch_reid(cur) == cur._rows
    sql = cur.executed[0]
    assert "CORE.CORE_SQUADS hs" in sql and "CORE.CORE_SQUADS aws" in sql
    assert "hs.GENDER = 'MALE'" in sql and "aws.GENDER = 'MALE'" in sql
    # pure flips belong to fetch_reversed
    assert "NOT (f.HOME_SQUAD_ID = m.AWAYSQUADID AND f.AWAY_SQUAD_ID = m.HOMESQUADID)" in sql
    assert "FIXTURE_DATE" not in sql


def test_fetch_reid_include_womens_drops_gender_guard():
    cur = FakeCursor()
    fetch_reid(cur, include_womens=True)
    assert "GENDER" not in cur.executed[0]


def test_count_reid_blocked_is_the_negation_of_the_resolve_guard():
    class OneRow(FakeCursor):
        def fetchone(self):
            return (62,)

    cur = OneRow()
    assert count_reid_blocked(cur) == 62
    assert "NOT (" in cur.executed[0]
