import pytest
from sqlalchemy.orm import Session

from fplquant.engine.rates import (
    BASE_GOALS_AWAY,
    BASE_GOALS_HOME,
    compute_fixture_rates,
    compute_team_ratings,
    rates_for_fixture,
)
from tests.engine_helpers import make_fixture, make_league, make_player, make_stat, make_team


def test_with_no_matches_played_the_ratings_are_the_prior(db_session: Session) -> None:
    """Nothing has happened yet, so the fitted correction must be exactly 1.0 —
    otherwise the fixed-point fit is inventing signal from an empty record."""
    make_league(db_session, teams=4)

    ratings = compute_team_ratings(db_session)

    for rating in ratings.values():
        assert rating.matches_played == 0
        assert rating.credibility == 0.0
        assert rating.attack_home == pytest.approx(rating.prior_attack_home)
        assert rating.leak_home == pytest.approx(rating.prior_leak_home)


def test_preseason_zero_strength_columns_do_not_flatten_the_priors(db_session: Session) -> None:
    """FPL publishes `strength_attack_*` as zero for every club until a season
    is underway. A prior that only read those columns would rate all twenty
    clubs identically at exactly the point in the season where a prior is doing
    all the work — so squad value has to carry it instead."""
    rich = make_team(db_session, fpl_id=1, short_name="RCH", strength=3)
    poor = make_team(db_session, fpl_id=2, short_name="POR", strength=3)
    for index in range(15):
        make_player(db_session, rich, fpl_id=100 + index, now_cost=100)
        make_player(db_session, poor, fpl_id=200 + index, now_cost=40)

    ratings = compute_team_ratings(db_session)

    assert rich.strength_attack_home == 0  # the column really is empty
    assert ratings[rich.id].attack_home > ratings[poor.id].attack_home
    assert ratings[rich.id].leak_home < ratings[poor.id].leak_home


def test_the_coarse_overall_rating_is_used_when_it_has_any_spread(db_session: Session) -> None:
    strong = make_team(db_session, fpl_id=1, short_name="STR", strength=5)
    weak = make_team(db_session, fpl_id=2, short_name="WEA", strength=2)
    for index in range(15):  # identical squad value, so only the rating can separate them
        make_player(db_session, strong, fpl_id=100 + index, now_cost=60)
        make_player(db_session, weak, fpl_id=200 + index, now_cost=60)

    ratings = compute_team_ratings(db_session)

    assert ratings[strong.id].attack_home > ratings[weak.id].attack_home


def test_outscoring_the_model_raises_a_team_attack_rating(db_session: Session) -> None:
    teams = make_league(db_session, teams=4)
    home, away = teams[0], teams[1]
    before = compute_team_ratings(db_session)[home.id].attack_home

    for event in range(1, 6):
        make_fixture(
            db_session,
            home,
            away,
            fpl_id=500 + event,
            event=event,
            finished=True,
            home_score=5,
            away_score=0,
        )

    after = compute_team_ratings(db_session)[home.id]
    assert after.matches_played == 5
    assert after.credibility > 0
    assert after.attack_home > before
    assert after.leak_home < compute_team_ratings(db_session)[away.id].leak_home


def test_expected_goals_outweigh_the_scoreline(db_session: Session) -> None:
    """A side that created five goals' worth of chances and scored one is a
    better attack than the scoreline says. xG settles down over a handful of
    matches where goals take most of a season, so it carries the larger weight."""
    teams = make_league(db_session, teams=2)
    home, away = teams
    fixture = make_fixture(
        db_session, home, away, fpl_id=900, event=1, finished=True, home_score=1, away_score=0
    )
    lucky = compute_team_ratings(db_session)[home.id].attack_home

    for player in home.players:
        make_stat(
            db_session,
            player,
            round_number=1,
            expected_goals=0.35,
            fixture=fixture,
            was_home=True,
        )

    unlucky_no_more = compute_team_ratings(db_session)[home.id].attack_home
    assert unlucky_no_more > lucky


def test_home_advantage_shows_up_in_the_fixture_rates(db_session: Session) -> None:
    teams = make_league(db_session, teams=2)
    fixture = make_fixture(db_session, teams[0], teams[1], fpl_id=900, event=1)

    ratings = compute_team_ratings(db_session)
    pair = rates_for_fixture(fixture, ratings)

    assert pair is not None
    lambda_home, lambda_away = pair
    # Identical clubs, so the only asymmetry left is the venue.
    assert lambda_home > lambda_away
    assert lambda_home == pytest.approx(BASE_GOALS_HOME, rel=0.05)
    assert lambda_away == pytest.approx(BASE_GOALS_AWAY, rel=0.05)


def test_matches_in_the_same_round_are_equally_recent(db_session: Session) -> None:
    """Recency decays by gameweek, not by position in the fixture list. Decaying
    within a round would rate a club that played Friday night as less known
    than one that played Sunday, which is a fact about the broadcast schedule."""
    teams = make_league(db_session, teams=4)
    for index in range(0, 4, 2):
        make_fixture(
            db_session,
            teams[index],
            teams[index + 1],
            fpl_id=700 + index,
            event=1,
            finished=True,
            home_score=1,
            away_score=1,
        )

    ratings = compute_team_ratings(db_session)
    weights = {ratings[team.id].effective_matches for team in teams}
    assert weights == {1.0}


def test_fixture_rates_cover_every_fixture(db_session: Session) -> None:
    teams = make_league(db_session, teams=4)
    make_fixture(db_session, teams[0], teams[1], fpl_id=1, event=1)
    make_fixture(db_session, teams[2], teams[3], fpl_id=2, event=1)

    rates = compute_fixture_rates(db_session)

    assert len(rates) == 2
    for entry in rates.values():
        assert entry.lambda_home > 0
        assert entry.lambda_away > 0


def test_an_empty_database_produces_no_ratings(db_session: Session) -> None:
    assert compute_team_ratings(db_session) == {}
