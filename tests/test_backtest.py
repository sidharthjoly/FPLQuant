import datetime as dt

from sqlalchemy.orm import Session

from fplquant.backtest.hydrate import hydrate
from fplquant.backtest.replay import FPL_XP_METHOD, replay_round, run_backtest
from fplquant.engine.rates import (
    _MAX_MULTIPLIER,
    _MIN_MULTIPLIER,
    compute_team_ratings,
    played_fixtures,
)
from fplquant.models.orm import Fixture, HistoricalPlayerGameweek, Player, PlayerGameweekStat

SEASON = "2023-24"
KICKOFF = dt.datetime(2023, 8, 12, 14, 0, tzinfo=dt.UTC)


# Arsenal are deliberately the better side here, and by a wide margin. A
# symmetric fixture cannot test a rating fit at all: with identical squads,
# prices and scorelines on both sides, a fit that works and a fit that silently
# does nothing produce exactly the same ratings, and every assertion passes
# either way. That is not hypothetical — it is why the whole team-rating layer
# could sit switched off through 131 replayed gameweeks without a red test.
_HOME_GOALS = 3
_AWAY_GOALS = 0


def _season(session: Session, rounds: int = 8, per_team: int = 15) -> None:
    """Two clubs playing each other every round, with full squads.

    Arsenal win 3-0 every time and are priced higher, so a fitted rating has
    something to find.
    """
    positions = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
    for round_number in range(1, rounds + 1):
        for home, (team, opponent_id) in enumerate([("Arsenal", 2), ("Chelsea", 1)]):
            strong = team == "Arsenal"
            for index in range(per_team):
                element = (1 if strong else 100) + index
                playing = index < 11
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
                        minutes=90 if playing else 0,
                        starts=1 if playing else 0,
                        total_points=(8 if strong else 2) if playing else 0,
                        goals_scored=1 if strong and playing and index >= 12 else 0,
                        expected_goals=(0.4 if strong else 0.05) if playing else 0.0,
                        team_h_score=_HOME_GOALS,
                        team_a_score=_AWAY_GOALS,
                        value=70 if strong else 45,
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


def test_a_replay_scores_the_engine_against_a_point_in_time_baseline(
    db_session: Session,
) -> None:
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    result = replay_round(rows, SEASON, 6)

    assert result is not None
    # The archive's xP is absent unless asked for: it saw the results it is
    # nominally forecasting, so it is not a baseline and must not sit in a
    # table next to one.
    assert set(result.scores) == {"engine", "rolling_mean"}
    assert result.players > 0
    for score in result.scores.values():
        assert score.mean_absolute_error >= 0
        assert -1.0 <= score.rank_correlation <= 1.0


def test_the_leaky_xp_column_is_opt_in_and_named_as_leaky(db_session: Session) -> None:
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    result = replay_round(rows, SEASON, 6, include_fpl_xp=True)

    assert result is not None
    assert FPL_XP_METHOD in result.scores
    assert "fpl_xp" not in result.scores  # the name that read as a fair baseline


def test_the_replay_pool_does_not_depend_on_the_leaky_column(db_session: Session) -> None:
    """Scoring only where xP has a value let a contaminated column choose the
    population every other method was judged on."""
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()
    for row in rows:
        if row.round == 6 and row.element in (1, 2, 3):
            row.expected_points = None
    db_session.flush()

    without = replay_round(rows, SEASON, 6)
    with_xp = replay_round(rows, SEASON, 6, include_fpl_xp=True)

    assert without is not None and with_xp is not None
    assert without.players == with_xp.players + 3


def test_only_players_every_method_can_score_are_compared(db_session: Session) -> None:
    """Judging each method on its own subset would let one look better simply
    by declining to predict the hard cases. Applies once xP is in the run."""
    _season(db_session)
    rows = db_session.query(HistoricalPlayerGameweek).all()
    for row in rows:
        if row.round == 6 and row.element == 1:
            row.expected_points = None  # FPL published no projection for him
    db_session.flush()

    result = replay_round(rows, SEASON, 6, include_fpl_xp=True)

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
    assert "engine" in summary and "rolling_mean" in summary


def test_backtesting_without_an_archive_returns_nothing(db_session: Session) -> None:
    assert run_backtest(db_session, ["1999-00"]).rounds == []


def test_hydration_gives_the_goal_model_something_to_fit(db_session: Session) -> None:
    """The regression test for the bug that made the whole backtest meaningless.

    `hydrate` used to mark past fixtures finished but leave their scoreline
    NULL, and `played_fixtures` requires both. So the goal model saw an empty
    match record, every club's credibility stayed at zero, the fitted
    correction collapsed to 1.0, and all 131 replayed gameweeks measured
    nothing but the preseason prior — while producing numbers that looked
    entirely plausible.

    The assertion that matters is the last one. Checking that ratings *exist*,
    or that they are finite, or that projections come out sane, all passed
    happily throughout: a prior is a perfectly sane rating. Only checking that
    the record actually *moved* them catches it.
    """
    _season(db_session, rounds=12)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    session, _ = hydrate(rows, up_to_round=10)
    try:
        played = played_fixtures(session)
        assert played, "no fixture counted as played — the goal model has no data"
        assert all(f.team_h_score is not None for f in played)

        ratings = compute_team_ratings(session)
        assert all(r.credibility > 0 for r in ratings.values())
        assert any(
            abs(r.attack_home - r.prior_attack_home) > 1e-9 for r in ratings.values()
        ), "every fitted rating equals its prior — the fit ran but changed nothing"
    finally:
        session.close()


def test_the_fitted_ratings_separate_a_strong_side_from_a_weak_one(
    db_session: Session,
) -> None:
    """Arsenal beat Chelsea 3-0 twelve times. Any working fit says so."""
    _season(db_session, rounds=12)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    session, _ = hydrate(rows, up_to_round=10)
    try:
        ratings = {r.short_name: r for r in compute_team_ratings(session).values()}
        arsenal, chelsea = ratings["ARS"], ratings["CHE"]
        assert arsenal.attack_home > chelsea.attack_home
        assert arsenal.leak_home < chelsea.leak_home
    finally:
        session.close()


def test_the_rating_fit_converges_instead_of_running_to_the_clamps(
    db_session: Session,
) -> None:
    """The multiplicative model is unidentified along a global scale direction,
    and attack and leak are corrected from the same state in the same pass — so
    the iteration walks that direction with gain, oscillating wider each pass
    until every multiplier pins at its clamp. It was stable only while the
    credibility weight could not exceed 0.5, which is to say by accident.
    """
    _season(db_session, rounds=20)
    rows = db_session.query(HistoricalPlayerGameweek).all()

    session, _ = hydrate(rows, up_to_round=18)
    try:
        ratings = compute_team_ratings(session)
        for rating in ratings.values():
            for value in (rating.attack_home, rating.attack_away, rating.leak_home):
                assert (
                    _MIN_MULTIPLIER < value < _MAX_MULTIPLIER
                ), f"{rating.short_name} pinned at a clamp: the fit diverged"
    finally:
        session.close()


def test_a_double_gameweek_keeps_both_fixtures(db_session: Session) -> None:
    """The archive stores one row per fixture and so, now, does the replay."""
    _season(db_session, rounds=8)
    extra = [
        HistoricalPlayerGameweek(
            season=SEASON,
            element=element,
            round=4,
            fixture=999,  # a second match in round 4
            name=f"P{element}",
            position="MID",
            team="Arsenal",
            opponent_team=2,
            was_home=True,
            kickoff_time=KICKOFF + dt.timedelta(days=30),
            minutes=90,
            starts=1,
            total_points=7,
            team_h_score=1,
            team_a_score=1,
            value=70,
            selected=1000,
            expected_points=4.0,
        )
        for element in (1, 2, 3)
    ]
    db_session.add_all(extra)
    db_session.flush()
    rows = db_session.query(HistoricalPlayerGameweek).all()

    session, player_ids = hydrate(rows, up_to_round=6)
    try:
        stats = session.query(PlayerGameweekStat).filter_by(player_id=player_ids[1], round=4).all()
        assert len(stats) == 2
        assert {s.fixture_fpl_id for s in stats} == {4, 999}
        assert sum(s.minutes for s in stats) == 180
    finally:
        session.close()
