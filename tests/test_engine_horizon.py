import pytest
from sqlalchemy.orm import Session

from fplquant.engine.horizon import project_horizon
from fplquant.schedule import get_upcoming_fixtures_by_team_event, upcoming_events
from tests.engine_helpers import make_fixture, make_league, make_round


def test_a_double_gameweek_sums_both_fixtures(db_session: Session) -> None:
    """The single biggest swing available to an FPL manager, and one a
    next-fixture model cannot represent at all."""
    teams = make_league(db_session, teams=4)
    make_fixture(db_session, teams[0], teams[1], fpl_id=1, event=1)
    make_fixture(db_session, teams[2], teams[3], fpl_id=2, event=1)
    # Same round, second fixture for the first pair.
    make_fixture(db_session, teams[1], teams[0], fpl_id=3, event=1)

    projections = project_horizon(db_session, horizon=1)

    doubled = next(p for p in projections if p.team_id == teams[0].id)
    single = next(p for p in projections if p.team_id == teams[2].id)
    assert doubled.events[0].is_double
    assert len(doubled.events[0].fixtures) == 2
    assert not single.events[0].is_double
    assert doubled.events[0].points == pytest.approx(
        sum(f.points for f in doubled.events[0].fixtures)
    )


def test_a_blank_gameweek_scores_nothing(db_session: Session) -> None:
    teams = make_league(db_session, teams=4)
    make_fixture(db_session, teams[0], teams[1], fpl_id=1, event=1)
    # teams[2] and teams[3] have no fixture in this round at all.
    make_fixture(db_session, teams[2], teams[3], fpl_id=2, event=2)

    projections = project_horizon(db_session, horizon=1)

    blanking = next(p for p in projections if p.team_id == teams[2].id)
    assert blanking.events[0].is_blank
    assert blanking.events[0].points == 0.0
    assert blanking.total_points == 0.0


def test_later_gameweeks_are_discounted(db_session: Session) -> None:
    """A projection five weeks out is less reliable than one for Saturday, and
    you get to revise the squad before it arrives."""
    teams = make_league(db_session, teams=4)
    for event in (1, 2, 3):
        make_round(db_session, teams, event)

    discounted = project_horizon(db_session, horizon=3, decay=0.5)
    undiscounted = project_horizon(db_session, horizon=3, decay=1.0)

    best = discounted[0]
    same = next(p for p in undiscounted if p.player_id == best.player_id)
    assert best.discounted_points < best.total_points
    assert same.discounted_points == pytest.approx(same.total_points)


def test_the_horizon_covers_only_rounds_that_still_have_football_in_them(
    db_session: Session,
) -> None:
    teams = make_league(db_session, teams=4)
    make_fixture(
        db_session, teams[0], teams[1], fpl_id=1, event=1, finished=True, home_score=1, away_score=0
    )
    make_fixture(db_session, teams[0], teams[1], fpl_id=2, event=4)
    make_fixture(db_session, teams[2], teams[3], fpl_id=3, event=7)

    assert upcoming_events(db_session, horizon=5) == [4, 7]


def test_a_postponed_fixture_is_not_planned_into_any_round(db_session: Session) -> None:
    """A match FPL has not rescheduled has no round to be planned into, and
    guessing one would put points in a gameweek that may never happen."""
    teams = make_league(db_session, teams=2)
    postponed = make_fixture(db_session, teams[0], teams[1], fpl_id=1, event=1)
    postponed.event = None
    db_session.flush()

    assert get_upcoming_fixtures_by_team_event(db_session) == {}
    assert upcoming_events(db_session, horizon=5) == []


def test_a_zero_horizon_is_rejected(db_session: Session) -> None:
    with pytest.raises(ValueError):
        upcoming_events(db_session, horizon=0)


def test_an_impossible_decay_is_rejected(db_session: Session) -> None:
    make_league(db_session, teams=2)
    with pytest.raises(ValueError):
        project_horizon(db_session, horizon=1, decay=0.0)
