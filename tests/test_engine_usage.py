import pytest
from sqlalchemy.orm import Session

from fplquant.engine.usage import ASSISTED_GOAL_FRACTION, compute_player_usage, fixture_inputs
from tests.engine_helpers import FWD, MID, make_league, make_player, make_stat, make_team


def test_a_clubs_goal_shares_add_up_to_all_of_its_goals(db_session: Session) -> None:
    """The point of a top-down model: sum a club's players' expected goals for
    a fixture and you get the club's expected goals back. A bottom-up model has
    no reason to satisfy this and generally doesn't."""
    teams = make_league(db_session, teams=2)

    usage = compute_player_usage(db_session)

    for team in teams:
        total = sum(u.goal_share for u in usage.values() if u.team_id == team.id)
        assert total == pytest.approx(1.0)


def test_the_expensive_striker_takes_the_biggest_share(db_session: Session) -> None:
    """With no matches played, price is the only evidence about who scores —
    and it is real evidence, not a placeholder."""
    teams = make_league(db_session, teams=2)
    forwards = [
        u
        for u in compute_player_usage(db_session).values()
        if u.team_id == teams[0].id and u.element_type == FWD
    ]

    ranked = sorted(forwards, key=lambda u: -u.now_cost)
    assert ranked[0].goal_share > ranked[-1].goal_share


def test_a_players_own_record_takes_over_from_the_price_prior(db_session: Session) -> None:
    teams = make_league(db_session, teams=2)
    cheap = min((p for p in teams[0].players if p.element_type == MID), key=lambda p: p.now_cost)
    before = compute_player_usage(db_session)[cheap.id]

    for round_number in range(1, 9):
        make_stat(db_session, cheap, round_number=round_number, minutes=90, expected_goals=0.8)
    db_session.expire_all()

    after = compute_player_usage(db_session)[cheap.id]
    assert before.rate_credibility == 0.0
    assert after.rate_credibility > 0.5
    assert after.goals_per_90 > before.goals_per_90
    assert after.goal_share > before.goal_share


def test_a_player_who_cannot_play_takes_no_share_of_the_goals(db_session: Session) -> None:
    team = make_team(db_session, fpl_id=1, short_name="AAA")
    fit = make_player(db_session, team, fpl_id=1, element_type=FWD, now_cost=100)
    injured = make_player(
        db_session,
        team,
        fpl_id=2,
        element_type=FWD,
        now_cost=100,
        status="i",
        chance_of_playing_next_round=0,
    )

    usage = compute_player_usage(db_session)

    assert usage[injured.id].goal_share == 0.0
    assert usage[fit.id].goal_share == pytest.approx(1.0)


def test_fixture_inputs_scale_a_share_by_the_fixture_goal_rate(db_session: Session) -> None:
    """The join between the two halves of the model: the same player is worth
    more against a defence the team model expects to leak."""
    make_league(db_session, teams=2)
    usage = next(iter(compute_player_usage(db_session).values()))

    easy = fixture_inputs(usage, lambda_for=2.4, lambda_against=0.8)
    hard = fixture_inputs(usage, lambda_for=0.9, lambda_against=2.4)

    assert easy.expected_goals == pytest.approx(2.4 * usage.goal_share)
    assert easy.expected_goals > hard.expected_goals
    assert easy.expected_assists == pytest.approx(2.4 * ASSISTED_GOAL_FRACTION * usage.assist_share)
    assert easy.lambda_conceded < hard.lambda_conceded


def test_an_empty_database_produces_no_usage(db_session: Session) -> None:
    assert compute_player_usage(db_session) == {}
