import pytest
from sqlalchemy.orm import Session

from fplquant.engine.minutes import (
    MAX_START_PROBABILITY,
    _normalise_to_slots,
    compute_minutes_profiles,
)
from tests.engine_helpers import DEF, FWD, GKP, MID, make_league, make_player, make_stat, make_team


def test_a_position_group_starts_as_many_players_as_it_has_slots(db_session: Session) -> None:
    """The constraint that makes this an absolute probability rather than a
    relative nudge: a club names eleven players, so a position group's start
    probabilities have to add up to the slots its formation fills."""
    make_league(db_session, teams=2)

    profiles = compute_minutes_profiles(db_session)

    by_group: dict[tuple[int, int], float] = {}
    for profile in profiles.values():
        key = (profile.team_id, profile.element_type)
        by_group[key] = by_group.get(key, 0.0) + profile.p_start

    # No matches played, so every club is on the 4-4-2 prior.
    expected = {GKP: 1.0, DEF: 4.0, MID: 4.0, FWD: 2.0}
    for (_, position), total in by_group.items():
        assert total == pytest.approx(expected[position], abs=0.01)


def test_the_more_expensive_player_is_the_one_who_starts(db_session: Session) -> None:
    team = make_team(db_session, fpl_id=1, short_name="AAA")
    first_choice = make_player(db_session, team, fpl_id=1, element_type=GKP, now_cost=55)
    understudy = make_player(db_session, team, fpl_id=2, element_type=GKP, now_cost=40)

    profiles = compute_minutes_profiles(db_session)

    assert profiles[first_choice.id].p_start > profiles[understudy.id].p_start
    assert profiles[first_choice.id].expected_minutes > profiles[understudy.id].expected_minutes


def test_an_injured_players_minutes_go_to_his_teammates(db_session: Session) -> None:
    """Availability is applied before the group is normalised, so ruling a
    first choice out promotes whoever is behind him instead of leaving the
    club a man short."""
    teams = make_league(db_session, teams=2)
    forwards = [p for p in teams[0].players if p.element_type == FWD]
    understudy = min(forwards, key=lambda p: p.now_cost)
    before = compute_minutes_profiles(db_session)[understudy.id].p_start

    injured = max(forwards, key=lambda p: p.now_cost)
    injured.status = "i"
    injured.chance_of_playing_next_round = 0
    db_session.flush()

    after = compute_minutes_profiles(db_session)
    assert after[injured.id].p_start == 0.0
    assert after[understudy.id].p_start > before


def test_a_start_probability_never_reaches_certainty(db_session: Session) -> None:
    team = make_team(db_session, fpl_id=1, short_name="AAA")
    only_keeper = make_player(db_session, team, fpl_id=1, element_type=GKP, now_cost=50)

    profiles = compute_minutes_profiles(db_session)

    assert profiles[only_keeper.id].p_start == pytest.approx(MAX_START_PROBABILITY)


def test_starting_every_week_beats_the_price_prior(db_session: Session) -> None:
    teams = make_league(db_session, teams=2)
    midfielders = sorted(
        (p for p in teams[0].players if p.element_type == MID), key=lambda p: p.now_cost
    )
    cheapest = midfielders[0]
    before = compute_minutes_profiles(db_session)[cheapest.id].p_start

    for round_number in range(1, 7):
        make_stat(db_session, cheapest, round_number=round_number, minutes=90, starts=1)
    db_session.expire_all()  # the first call above cached an empty stat collection

    after = compute_minutes_profiles(db_session)[cheapest.id]
    assert after.p_start > before
    assert after.start_credibility > 0.5
    assert after.observed_start_rate == 1.0


def test_normalise_to_slots_redistributes_what_the_cap_spills() -> None:
    """A plain rescale would push the top player past the cap and clipping
    would lose the excess, leaving the group short. Water-filling hands it to
    whoever is left."""
    result = _normalise_to_slots([10.0, 1.0, 1.0], slots=2.0, cap=0.9)

    assert result[0] == pytest.approx(0.9)
    assert sum(result) == pytest.approx(2.0)
    assert result[1] == result[2]


def test_normalise_to_slots_handles_a_group_with_nothing_in_it() -> None:
    assert _normalise_to_slots([], slots=2.0, cap=0.9) == []
    assert _normalise_to_slots([0.0, 0.0], slots=2.0, cap=0.9) == [0.0, 0.0]
