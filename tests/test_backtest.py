import datetime as dt

from sqlalchemy.orm import Session

from fplquant.backtest.hydrate import hydrate
from fplquant.backtest.replay import replay_round, run_backtest
from fplquant.models.orm import Fixture, HistoricalPlayerGameweek, Player, PlayerGameweekStat

SEASON = "2023-24"
KICKOFF = dt.datetime(2023, 8, 12, 14, 0, tzinfo=dt.UTC)


def _season(session: Session, rounds: int = 8, per_team: int = 15) -> None:
    """Two clubs playing each other every round, with full squads."""
    positions = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    for round_number in range(1, rounds + 1):
        for home, (team, opponent_id) in enumerate([("Arsenal", 2), ("Chelsea", 1)]):
            for index in range(per_team):
                element = (1 if team == "Arsenal" else 100) + index
                session.add(
                    HistoricalPlayerGameweek(
                        season=SEASON,
                        element=element,
                        round=round_number,
                        fixture=round_number,
                        name=f"P{element}",
                        position=positions[index % len(positions)],
                        team=team,
                        opponent_team=opponent_id,
                        was_home=home == 0,
                        kickoff_time=KICKOFF + dt.timedelta(days=7 * round_number),
                        minutes=90 if index < 11 else 0,
                        starts=1 if index < 11 else 0,
                        total_points=6 if index < 11 else 0,
                        value=60,
                        selected=1000,
                        expected_points=4.0,
                    )
                )
    session.flush()


def test_hydration_stops_at_the_round_being_predicted(db_session: Session) -> None:
    """The whole point: a replay of gameweek 5 must not be able to see gameweek
    5, or anything after it."""
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    session, _ = hydrate(rows, up_to_round=5)

    rounds = {stat.round for stat in session.query(PlayerGameweekStat).all()}
    assert rounds == {1, 2, 3, 4}
    played = {f.event for f in session.query(Fixture).filter(Fixture.finished.is_(True))}
    upcoming = {f.event for f in session.query(Fixture).filter(Fixture.finished.is_(False))}
    assert played == {1, 2, 3, 4}
    assert upcoming == {5}


def test_hydration_rebuilds_the_clubs_and_squads(db_session: Session) -> None:
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    session, player_ids = hydrate(rows, up_to_round=5)

    assert session.query(Player).count() == 30
    assert len(player_ids) == 30
    # Positions survive the round trip into element types.
    assert {p.element_type for p in session.query(Player).all()} == {1, 2, 3, 4}


def test_a_players_price_comes_from_before_the_deadline(db_session: Session) -> None:
    """Price is only known once that gameweek's row exists, so taking it from
    the round being predicted would read a number from the future."""
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()
    for row in rows:
        if row.round >= 5:
            row.value = 999
    db_session.flush()

    session, _ = hydrate(rows, up_to_round=5)

    assert all(player.now_cost != 999 for player in session.query(Player).all())


def test_a_replay_scores_the_engine_against_fpls_own_projection(db_session: Session) -> None:
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    result = replay_round(rows, SEASON, 6)

    assert result is not None
    assert set(result.scores) == {"engine", "fpl_xp", "rolling_mean"}
    assert result.players > 0
    for score in result.scores.values():
        assert score.mean_absolute_error >= 0
        assert -1.0 <= score.rank_correlation <= 1.0


def test_only_players_every_method_can_score_are_compared(db_session: Session) -> None:
    """Judging each method on its own subset would let one look better simply
    by declining to predict the hard cases."""
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()
    for row in rows:
        if row.round == 6 and row.element == 1:
            row.expected_points = None  # FPL published no projection for him
    db_session.flush()

    result = replay_round(rows, SEASON, 6)

    assert result is not None
    assert result.players == 29  # the unscoreable player is dropped for everyone


def test_a_round_nobody_played_is_skipped(db_session: Session) -> None:
    _season(db_session, rounds=4)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    assert replay_round(rows, SEASON, 30) is None


def test_the_summary_averages_across_gameweeks(db_session: Session) -> None:
    _season(db_session)
    db_session.flush()

    result = run_backtest(db_session, [SEASON], first_round=5, last_round=7)

    assert len(result.rounds) >= 1
    summary = result.summary()
    assert "engine" in summary and "fpl_xp" in summary


def test_backtesting_without_an_archive_returns_nothing(db_session: Session) -> None:
    assert run_backtest(db_session, ["1999-00"]).rounds == []
