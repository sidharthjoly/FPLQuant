import datetime as dt

from sqlalchemy.orm import Session

from fplquant.data import ingest
from fplquant.models.orm import (
    Fixture,
    Player,
    PlayerGameweekStat,
    PlayerSnapshot,
    Team,
    TeamSnapshot,
)

from .data.fixtures import (
    ELEMENTS_PAYLOAD,
    FIXTURES_PAYLOAD,
    PLAYER_HISTORY_PAYLOAD,
    TEAMS_PAYLOAD,
)


def test_upsert_teams_creates_then_updates(db_session: Session) -> None:
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    assert len(teams) == 2
    assert db_session.query(Team).count() == 2

    updated_payload = [{**TEAMS_PAYLOAD[0], "name": "Arsenal FC"}, TEAMS_PAYLOAD[1]]
    ingest.upsert_teams(db_session, updated_payload)
    assert db_session.query(Team).count() == 2  # no duplicates
    assert db_session.query(Team).filter_by(fpl_id=1).one().name == "Arsenal FC"


def test_upsert_players_links_to_team(db_session: Session) -> None:
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    players = ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)

    assert db_session.query(Player).count() == 1
    player = players[101]
    assert player.web_name == "Raya"
    assert player.team.short_name == "ARS"
    assert player.selected_by_percent == 31.2
    assert player.code == 154561


def test_upsert_fixtures(db_session: Session) -> None:
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    ingest.upsert_fixtures(db_session, FIXTURES_PAYLOAD, teams)

    assert db_session.query(Fixture).count() == 1
    fixture = db_session.query(Fixture).one()
    assert fixture.team_h.short_name == "ARS"
    assert fixture.team_a.short_name == "CHE"
    assert fixture.event == 1


def test_upsert_player_gameweek_stats_is_idempotent(db_session: Session) -> None:
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    players = ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)
    player = players[101]

    ingest.upsert_player_gameweek_stats(db_session, player, PLAYER_HISTORY_PAYLOAD)
    ingest.upsert_player_gameweek_stats(db_session, player, PLAYER_HISTORY_PAYLOAD)

    assert db_session.query(PlayerGameweekStat).count() == 1
    stat = db_session.query(PlayerGameweekStat).one()
    assert stat.total_points == 6
    assert stat.expected_goals_conceded == 0.8


def test_a_provisionally_finished_fixture_counts_as_played(db_session: Session) -> None:
    """FPL leaves `finished` false until bonus points are confirmed, which can
    lag the final whistle by a day. Without reading `finished_provisional` too,
    a gameweek that has just been played still looks upcoming, and every
    "next fixture" lookup resolves to a match already in the books."""
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    payload = [
        {
            **FIXTURES_PAYLOAD[0],
            "finished": False,
            "finished_provisional": True,
        }
    ]

    ingest.upsert_fixtures(db_session, payload, teams)

    assert db_session.query(Fixture).one().finished is True


def test_snapshots_capture_the_fields_ingest_overwrites(db_session: Session) -> None:
    """These six fields are overwritten on every ingest and FPL publishes no
    history for them, so a backtest cannot reconstruct them after the fact.
    That makes the archive the only copy — and one that has to start
    collecting before it is wanted."""
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)

    ingest.record_snapshots(db_session)

    snapshot = db_session.query(PlayerSnapshot).one()
    player = db_session.query(Player).one()
    assert snapshot.player_id == player.id
    assert snapshot.now_cost == player.now_cost
    assert snapshot.ep_next == player.ep_next
    assert snapshot.status == player.status
    assert snapshot.chance_of_playing_next_round == player.chance_of_playing_next_round
    assert snapshot.form == player.form
    assert snapshot.selected_by_percent == player.selected_by_percent


def test_a_snapshot_survives_the_ingest_that_overwrites_the_player(
    db_session: Session,
) -> None:
    """The whole point: yesterday's price and availability stay readable after
    today's ingest has replaced them on the player row."""
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)
    yesterday = dt.datetime(2026, 8, 24, 3, 0, tzinfo=dt.UTC)
    ingest.record_snapshots(db_session, captured_at=yesterday)

    ingest.upsert_players(
        db_session,
        [
            {
                **ELEMENTS_PAYLOAD[0],
                "now_cost": 999,
                "status": "i",
                "chance_of_playing_next_round": 0,
            }
        ],
        teams,
    )

    player = db_session.query(Player).one()
    snapshot = db_session.query(PlayerSnapshot).one()
    assert player.now_cost == 999 and player.status == "i"
    assert snapshot.now_cost != 999
    assert snapshot.status == "a"


def test_recording_twice_in_a_day_updates_rather_than_duplicates(db_session: Session) -> None:
    """Prices move once a day and the ingest can be re-run, so the day is the
    key. Appending instead would grow the table without adding information."""
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)
    morning = dt.datetime(2026, 8, 25, 3, 0, tzinfo=dt.UTC)
    evening = dt.datetime(2026, 8, 25, 21, 0, tzinfo=dt.UTC)

    ingest.record_snapshots(db_session, captured_at=morning)
    db_session.query(Player).one().now_cost = 123
    db_session.flush()
    ingest.record_snapshots(db_session, captured_at=evening)

    snapshot = db_session.query(PlayerSnapshot).one()  # one row, not two
    assert snapshot.now_cost == 123  # the later read wins
    # SQLite stores no timezone, so this comes back naive — same instant.
    assert snapshot.captured_at.replace(tzinfo=dt.UTC) == evening


def test_a_new_day_gets_its_own_row(db_session: Session) -> None:
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)

    ingest.record_snapshots(db_session, captured_at=dt.datetime(2026, 8, 25, 3, 0, tzinfo=dt.UTC))
    ingest.record_snapshots(db_session, captured_at=dt.datetime(2026, 8, 26, 3, 0, tzinfo=dt.UTC))

    assert db_session.query(PlayerSnapshot).count() == 2


def test_snapshots_record_the_gameweek_they_lead_into(db_session: Session) -> None:
    """A backtest asks for "the state going into GW5"; storing it here saves
    re-deriving it from a fixture calendar that will have moved on."""
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)
    ingest.upsert_fixtures(db_session, [{**FIXTURES_PAYLOAD[0], "event": 3}], teams)

    ingest.record_snapshots(db_session)

    assert db_session.query(PlayerSnapshot).one().next_event == 3
    assert db_session.query(TeamSnapshot).first().next_event == 3


def test_team_strengths_are_archived_too(db_session: Session) -> None:
    """Currently zero for every club, which is itself the thing worth
    recording — it is why `engine.rates` falls back to squad value, and a
    backtest needs to know which prior was in play that week."""
    ingest.upsert_teams(db_session, TEAMS_PAYLOAD)

    ingest.record_snapshots(db_session)

    snapshots = db_session.query(TeamSnapshot).all()
    assert len(snapshots) == db_session.query(Team).count()
    assert all(s.captured_on is not None for s in snapshots)


DOUBLE_GAMEWEEK_PAYLOAD = [
    {**PLAYER_HISTORY_PAYLOAD[0], "fixture": 1001, "minutes": 90, "total_points": 6},
    {
        **PLAYER_HISTORY_PAYLOAD[0],
        "fixture": 1002,  # same round, second match
        "opponent_team": 3,
        "was_home": False,
        "minutes": 75,
        "total_points": 9,
        "goals_scored": 1,
    },
]


def test_a_double_gameweek_keeps_both_matches(db_session: Session) -> None:
    """The bug this guards was silent and cost real data.

    FPL returns one history entry per *match*, so a double gameweek is two
    entries in the same round. The upsert keyed on the round alone, so the
    second simply overwrote the first — no error, no log line, and the player's
    first match of the week ceased to exist. Doubles carried between 2.6% and
    11% of all player-minutes across the last four seasons, concentrated in the
    rounds the planner most wants to get right.
    """
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    players = ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)
    player = players[101]

    ingest.upsert_player_gameweek_stats(db_session, player, DOUBLE_GAMEWEEK_PAYLOAD)
    ingest.upsert_player_gameweek_stats(db_session, player, DOUBLE_GAMEWEEK_PAYLOAD)  # idempotent

    stats = db_session.query(PlayerGameweekStat).all()
    assert len(stats) == 2
    assert {s.fixture_fpl_id for s in stats} == {1001, 1002}
    assert sum(s.minutes for s in stats) == 165
    assert sum(s.total_points for s in stats) == 15


def test_defensive_contribution_is_stored_and_absence_is_not_a_zero(
    db_session: Session,
) -> None:
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    players = ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)

    history = [
        {**PLAYER_HISTORY_PAYLOAD[0], "defensive_contribution": 11, "tackles": 4},
        # An older round, from a season before FPL counted defensive actions.
        {**PLAYER_HISTORY_PAYLOAD[0], "round": 2, "fixture": 1002},
    ]
    ingest.upsert_player_gameweek_stats(db_session, players[101], history)

    by_round = {s.round: s for s in db_session.query(PlayerGameweekStat).all()}
    assert by_round[1].defensive_contribution == 11
    assert by_round[1].tackles == 4
    # None, not 0: "nobody was counting" is not "made no defensive actions".
    assert by_round[2].defensive_contribution is None


def test_a_stale_database_says_so(db_session: Session) -> None:
    """A pipeline that quietly stops updating produces predictions that look
    entirely reasonable and are last week's. Nothing else in the stack notices."""
    teams = ingest.upsert_teams(db_session, TEAMS_PAYLOAD)
    players = ingest.upsert_players(db_session, ELEMENTS_PAYLOAD, teams)
    ingest.upsert_fixtures(db_session, FIXTURES_PAYLOAD, teams)
    for fixture in db_session.query(Fixture).all():
        fixture.finished = True
        fixture.event = 4
    ingest.upsert_player_gameweek_stats(db_session, players[101], PLAYER_HISTORY_PAYLOAD)
    db_session.flush()

    assert ingest.warn_if_stale(db_session) == 3  # fixtures through GW4, stats to GW1

    for fixture in db_session.query(Fixture).all():
        fixture.event = 1
    db_session.flush()
    assert ingest.warn_if_stale(db_session) is None
