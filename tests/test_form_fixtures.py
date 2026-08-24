import datetime as dt

import pytest
from sqlalchemy.orm import Session

from fplquant.form.fixtures import (
    chance_of_playing,
    compute_fixture_adjusted_scores,
)
from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team
from fplquant.schedule import get_next_fixture_by_team

GKP, DEF, MID, FWD = 1, 2, 3, 4


def _team(session: Session, fpl_id: int, short_name: str, **strengths: int) -> Team:
    team = Team(
        fpl_id=fpl_id,
        name=short_name,
        short_name=short_name,
        strength_attack_home=strengths.get("attack_home", 1000),
        strength_attack_away=strengths.get("attack_away", 1000),
        strength_defence_home=strengths.get("defence_home", 1000),
        strength_defence_away=strengths.get("defence_away", 1000),
    )
    session.add(team)
    session.flush()
    return team


def _player(session: Session, team: Team, *, fpl_id: int, web_name: str, **kwargs) -> Player:
    player = Player(
        fpl_id=fpl_id,
        team_id=team.id,
        first_name=web_name,
        second_name=web_name,
        web_name=web_name,
        element_type=kwargs.pop("element_type", MID),
        now_cost=60,
        status=kwargs.pop("status", "a"),
        ep_next=kwargs.pop("ep_next", 5.0),
        **kwargs,
    )
    session.add(player)
    session.flush()
    return player


def _fixture(
    session: Session,
    home: Team,
    away: Team,
    *,
    fpl_id: int,
    kickoff: dt.datetime,
    finished: bool = False,
    home_difficulty: int = 3,
    away_difficulty: int = 3,
) -> Fixture:
    fixture = Fixture(
        fpl_id=fpl_id,
        kickoff_time=kickoff,
        finished=finished,
        team_h_id=home.id,
        team_a_id=away.id,
        team_h_difficulty=home_difficulty,
        team_a_difficulty=away_difficulty,
    )
    session.add(fixture)
    session.flush()
    return fixture


def test_chance_of_playing_uses_explicit_percentage_regardless_of_status() -> None:
    player = Player(
        fpl_id=1,
        team_id=1,
        first_name="P",
        second_name="P",
        web_name="P",
        element_type=MID,
        now_cost=50,
        status="d",
        chance_of_playing_next_round=75,
    )
    assert chance_of_playing(player) == 0.75


def test_chance_of_playing_available_status_with_no_percent_is_fully_expected() -> None:
    player = Player(
        fpl_id=1,
        team_id=1,
        first_name="P",
        second_name="P",
        web_name="P",
        element_type=MID,
        now_cost=50,
        status="a",
        chance_of_playing_next_round=None,
    )
    assert chance_of_playing(player) == 1.0


def test_chance_of_playing_injured_with_no_percent_is_zero() -> None:
    player = Player(
        fpl_id=1,
        team_id=1,
        first_name="P",
        second_name="P",
        web_name="P",
        element_type=MID,
        now_cost=50,
        status="i",
        chance_of_playing_next_round=None,
    )
    assert chance_of_playing(player) == 0.0


def test_get_next_fixture_by_team_picks_the_earliest_unplayed(db_session: Session) -> None:
    home = _team(db_session, 1, "AAA")
    away = _team(db_session, 2, "BBB")
    _fixture(db_session, home, away, fpl_id=1, kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
    later = _fixture(
        db_session, home, away, fpl_id=2, kickoff=dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    )
    # An earlier fixture that's already finished shouldn't be picked.
    _fixture(
        db_session,
        home,
        away,
        fpl_id=0,
        kickoff=dt.datetime(2026, 8, 25, tzinfo=dt.UTC),
        finished=True,
    )

    next_by_team = get_next_fixture_by_team(db_session)

    assert next_by_team[home.id].fpl_id == 1
    assert next_by_team[away.id].fpl_id == 1
    assert next_by_team[home.id] != later


def test_forward_gets_a_higher_multiplier_against_a_weak_defence(db_session: Session) -> None:
    strong_defence = _team(db_session, 1, "STR", defence_away=1400)
    weak_defence = _team(db_session, 2, "WEA", defence_away=600)
    home_a = _team(db_session, 3, "HMA")
    home_b = _team(db_session, 4, "HMB")

    _fixture(
        db_session, home_a, strong_defence, fpl_id=1, kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    _fixture(
        db_session, home_b, weak_defence, fpl_id=2, kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    _player(db_session, home_a, fpl_id=1, web_name="VsStrong", element_type=FWD, ep_next=5.0)
    _player(db_session, home_b, fpl_id=2, web_name="VsWeak", element_type=FWD, ep_next=5.0)

    scores = {s.web_name: s for s in compute_fixture_adjusted_scores(db_session)}

    assert scores["VsWeak"].adjusted_points > scores["VsStrong"].adjusted_points


def test_defender_gets_a_lower_multiplier_against_a_strong_attack(db_session: Session) -> None:
    strong_attack = _team(db_session, 1, "STR", attack_away=1400)
    home = _team(db_session, 2, "HOM")
    _fixture(
        db_session, home, strong_attack, fpl_id=1, kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    _player(db_session, home, fpl_id=1, web_name="Def", element_type=DEF, ep_next=5.0)

    scores = {s.web_name: s for s in compute_fixture_adjusted_scores(db_session)}

    assert scores["Def"].fixture_multiplier is not None
    assert scores["Def"].fixture_multiplier < 1.0


def test_multiplier_is_clamped(db_session: Session) -> None:
    extreme_defence = _team(db_session, 1, "EXT", defence_away=100000)
    home = _team(db_session, 2, "HOM")
    _fixture(
        db_session, home, extreme_defence, fpl_id=1, kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    _player(db_session, home, fpl_id=1, web_name="Fwd", element_type=FWD, ep_next=5.0)

    scores = {s.web_name: s for s in compute_fixture_adjusted_scores(db_session)}

    assert scores["Fwd"].fixture_multiplier == pytest.approx(0.7)  # the floor, not near-zero


def test_home_away_picks_the_correct_difficulty_side(db_session: Session) -> None:
    home = _team(db_session, 1, "HOM")
    away = _team(db_session, 2, "AWA")
    _fixture(
        db_session,
        home,
        away,
        fpl_id=1,
        kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        home_difficulty=2,
        away_difficulty=5,
    )
    _player(db_session, home, fpl_id=1, web_name="HomePlayer")
    _player(db_session, away, fpl_id=2, web_name="AwayPlayer")

    scores = {s.web_name: s for s in compute_fixture_adjusted_scores(db_session)}

    assert scores["HomePlayer"].difficulty == 2
    assert scores["HomePlayer"].is_home is True
    assert scores["AwayPlayer"].difficulty == 5
    assert scores["AwayPlayer"].is_home is False
    assert scores["HomePlayer"].opponent_short_name == "AWA"
    assert scores["AwayPlayer"].opponent_short_name == "HOM"


def test_no_scheduled_fixture_falls_back_to_base_points_times_play_probability(
    db_session: Session,
) -> None:
    team = _team(db_session, 1, "SOL")
    _player(db_session, team, fpl_id=1, web_name="NoFixture", ep_next=8.0, status="a")

    scores = {s.web_name: s for s in compute_fixture_adjusted_scores(db_session)}

    assert scores["NoFixture"].adjusted_points == pytest.approx(8.0)
    assert scores["NoFixture"].fixture_multiplier is None
    assert scores["NoFixture"].opponent_short_name is None


def test_injured_player_has_near_zero_adjusted_points_despite_good_fixture(
    db_session: Session,
) -> None:
    weak_defence = _team(db_session, 1, "WEA", defence_away=500)
    home = _team(db_session, 2, "HOM")
    _fixture(
        db_session, home, weak_defence, fpl_id=1, kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    _player(
        db_session, home, fpl_id=1, web_name="Injured", element_type=FWD, ep_next=10.0, status="i"
    )

    scores = {s.web_name: s for s in compute_fixture_adjusted_scores(db_session)}

    assert scores["Injured"].adjusted_points == 0.0


def test_adjusted_points_matches_the_formula(db_session: Session) -> None:
    weak_defence = _team(db_session, 1, "WEA", defence_away=500)
    home = _team(db_session, 2, "HOM")
    _fixture(
        db_session, home, weak_defence, fpl_id=1, kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    _player(
        db_session,
        home,
        fpl_id=1,
        web_name="Fwd",
        element_type=FWD,
        ep_next=10.0,
        status="d",
        chance_of_playing_next_round=50,
    )

    score = compute_fixture_adjusted_scores(db_session)[0]

    assert score.adjusted_points == pytest.approx(
        score.base_points * score.fixture_multiplier * score.chance_of_playing
    )
    assert score.chance_of_playing == 0.5


def test_players_with_gameweek_history_use_form_not_ep_next(db_session: Session) -> None:
    team = _team(db_session, 1, "HOM")
    player = _player(db_session, team, fpl_id=1, web_name="HasForm", ep_next=1.0)
    for round_number, pts in enumerate([10, 10, 10], start=1):
        db_session.add(
            PlayerGameweekStat(
                player_id=player.id, round=round_number, minutes=90, total_points=pts
            )
        )
    db_session.flush()

    score = compute_fixture_adjusted_scores(db_session)[0]

    # Three appearances is a third of the way to full credibility, so the
    # estimate has moved most of the way from the low ep_next toward the
    # points history without landing on it: 10 * 3/9 + 1.0 * 6/9.
    assert score.base_points == pytest.approx(4.0)


def test_precomputed_lineup_multipliers_are_used_instead_of_recomputing(
    db_session: Session,
) -> None:
    """Callers that already hold the start-probability breakdown pass the
    multipliers straight in rather than paying for a second walk over every
    player's gameweek history."""
    team = _team(db_session, fpl_id=1, short_name="ARS")
    opponent = _team(db_session, fpl_id=2, short_name="CHE")
    player = _player(db_session, team, fpl_id=1, web_name="Player", ep_next=5.0)
    _fixture(
        db_session,
        home=team,
        away=opponent,
        fpl_id=1,
        kickoff=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    db_session.commit()

    default = next(s for s in compute_fixture_adjusted_scores(db_session))
    injected = next(
        s for s in compute_fixture_adjusted_scores(db_session, lineup_multipliers={player.id: 0.5})
    )

    assert default.lineup_multiplier == 1.0
    assert injected.lineup_multiplier == 0.5
    assert injected.adjusted_points == pytest.approx(default.adjusted_points * 0.5)
