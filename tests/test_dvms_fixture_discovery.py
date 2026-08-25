from python.extract.dvms import load_dvms_fixtures as loader


def _fixture(fixture_id, home, away):
    return {
        "fixtureId": fixture_id,
        "optaHomeTeamId": home,
        "optaAwayTeamId": away,
    }


def test_empty_competition_response_recursively_discovers_teams(monkeypatch):
    responses = {
        "t33": [_fixture("f1", "t33", "t99")],
        "t99": [_fixture("f1", "t33", "t99"), _fixture("f2", "t99", "t100")],
        "t100": [_fixture("f2", "t99", "t100")],
    }
    calls = []
    sleeps = []
    monkeypatch.setattr(loader.dvms_client, "iter_fixtures", lambda *args: iter(()))
    monkeypatch.setattr(
        loader.dvms_client,
        "get_team_fixtures",
        lambda _session, _competition, _season, team: calls.append(team) or responses.get(team, []),
    )
    monkeypatch.setattr(loader.time, "sleep", lambda seconds: sleeps.append(seconds))

    fixtures = loader._collect_all_fixtures(
        object(), "championship", "2026", bootstrap_team_ids={"t33"},
    )

    assert {fixture["fixtureId"] for fixture in fixtures} == {"f1", "f2"}
    assert calls == ["t33", "t99", "t100"]
    assert sleeps == [loader.config.DOWNLOAD_DELAY_SECONDS] * 2


def test_competition_seed_and_team_responses_are_deduplicated(monkeypatch):
    seed = _fixture("f1", "t1", "t2")
    calls = []
    monkeypatch.setattr(loader.dvms_client, "iter_fixtures", lambda *args: iter([seed]))
    monkeypatch.setattr(
        loader.dvms_client,
        "get_team_fixtures",
        lambda _session, _competition, _season, team: calls.append(team) or [seed],
    )
    monkeypatch.setattr(loader.time, "sleep", lambda _seconds: None)

    fixtures = loader._collect_all_fixtures(object(), "championship", "2026")

    assert fixtures == [seed]
    assert calls == ["t1", "t2"]


def test_all_empty_discovery_paths_return_no_fixtures(monkeypatch):
    monkeypatch.setattr(loader.dvms_client, "iter_fixtures", lambda *args: iter(()))
    monkeypatch.setattr(loader.dvms_client, "get_team_fixtures", lambda *args: [])

    assert loader._collect_all_fixtures(
        object(), "championship", "bad-season", bootstrap_team_ids={"t33"},
    ) == []


def test_load_known_team_ids_is_competition_scoped():
    class Cursor:
        def __init__(self):
            self.params = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql, params):
            self.params = params

        def fetchall(self):
            return [("t1",), ("t2",), (None,)]

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def cursor(self):
            return self.cursor_instance

    connection = Connection()

    result = loader._load_known_team_ids(connection, "DVMS_RAW", "championship")

    assert result == {"t1", "t2"}
    assert connection.cursor_instance.params == {"competition_id": "championship"}
