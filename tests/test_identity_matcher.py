from datetime import date

from python.identity.matcher import fetch_new_external_players


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


def test_fetch_new_external_players_trims_whitespace():
    cur = FakeCursor([
        ("299728", "Charlie Fletcher ", date(2010, 1, 5), "Charlie ", "Fletcher "),
    ])

    result = fetch_new_external_players(cur, "IMPECT")

    assert len(result) == 1
    ext = result[0]
    assert ext.source_name == "Charlie Fletcher"
    assert ext.source_first_name == "Charlie"
    assert ext.source_last_name == "Fletcher"
