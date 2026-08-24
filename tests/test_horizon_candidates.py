from sqlalchemy.orm import Session

from fplquant.optimizer.candidates import build_horizon_candidates_from_db
from tests.engine_helpers import make_league, make_round


def test_an_owned_player_survives_the_pool_trim(db_session: Session) -> None:
    """The pool is trimmed to the top of each position, and a player you
    already own has to come through it whatever their projection says. Dropping
    them makes the planner raise rather than plan, because the program has no
    way to express keeping a player it was never given."""
    teams = make_league(db_session, teams=6)
    for event in (1, 2):
        make_round(db_session, teams, event)

    trimmed, _ = build_horizon_candidates_from_db(
        db_session, horizon=2, pool_per_position={1: 1, 2: 1, 3: 1, 4: 1}
    )
    assert len(trimmed) == 4

    everyone, _ = build_horizon_candidates_from_db(db_session, horizon=2)
    excluded = next(
        candidate.player_id
        for candidate in everyone
        if candidate.player_id not in {c.player_id for c in trimmed}
    )
    forced, _ = build_horizon_candidates_from_db(
        db_session,
        horizon=2,
        pool_per_position={1: 1, 2: 1, 3: 1, 4: 1},
        always_include={excluded},
    )

    assert excluded in {candidate.player_id for candidate in forced}
    assert len(forced) == 5


def test_an_unavailable_player_is_left_out_unless_you_own_him(db_session: Session) -> None:
    teams = make_league(db_session, teams=4)
    make_round(db_session, teams, 1)
    departed = teams[0].players[0]
    departed.status = "u"  # left the club / not in FPL this season
    db_session.flush()

    without, _ = build_horizon_candidates_from_db(db_session, horizon=1)
    with_owned, _ = build_horizon_candidates_from_db(
        db_session, horizon=1, always_include={departed.id}
    )

    assert departed.id not in {candidate.player_id for candidate in without}
    assert departed.id in {candidate.player_id for candidate in with_owned}


def test_every_candidate_carries_a_points_estimate_for_every_gameweek(
    db_session: Session,
) -> None:
    """The planner indexes points by event, so a missing round would silently
    read as a blank gameweek rather than as missing data."""
    teams = make_league(db_session, teams=4)
    for event in (1, 2, 3):
        make_round(db_session, teams, event)

    candidates, events = build_horizon_candidates_from_db(db_session, horizon=3)

    assert events == [1, 2, 3]
    for candidate in candidates:
        assert set(candidate.points_by_event) == set(events)
