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


def test_redistributing_an_unavailable_players_share_keeps_the_group_total() -> None:
    """The model's probabilities are calibrated in absolute terms, so the group
    total is the model's own choice and has to survive. Only the mass an
    unavailable player gives up moves, and it moves to his teammates."""
    from fplquant.engine.minutes import _redistribute_unavailable

    unchanged = _redistribute_unavailable([0.4, 0.3, 0.2], [1.0, 1.0, 1.0], cap=0.97)
    assert unchanged == pytest.approx([0.4, 0.3, 0.2])

    injured = _redistribute_unavailable([0.4, 0.3, 0.2], [0.0, 1.0, 1.0], cap=0.97)
    assert injured[0] == 0.0
    assert sum(injured) == pytest.approx(0.9)  # the group total is preserved
    assert injured[1] > 0.3 and injured[2] > 0.2  # both teammates gain


def test_redistribution_still_respects_the_cap() -> None:
    """Preserving the total is not worth manufacturing a certainty. When the
    freed mass would push someone past the cap it is clipped, and the group
    ends up below its original total — which is the honest outcome."""
    from fplquant.engine.minutes import _redistribute_unavailable

    result = _redistribute_unavailable([0.9, 0.6, 0.3], [0.0, 1.0, 1.0], cap=0.97)

    assert max(result) <= 0.97
    assert sum(result) < 1.8


def test_the_model_path_does_not_impose_a_formation_prior(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 4-4-2 prior expects 1.83 forwards per club; a model trained on real
    football expects about one, because modern sides start a lone striker and
    FPL classifies wingers as midfielders. Normalising the model's output onto
    that prior scaled every forward in the league up by nearly a factor of two.

    Under the model, a group of equally-rated players must keep the level the
    model gave them rather than being stretched to fill the formation.
    """

    class _StubModel:
        def predict(self, rows: list[object]) -> list[float]:
            return [0.3] * len(rows)

    teams = make_league(db_session, teams=2)
    monkeypatch.setattr("fplquant.engine.minutes.load", lambda *a, **k: _StubModel())
    monkeypatch.setattr(
        "fplquant.engine.minutes._model_probabilities",
        lambda session, players, trained: {p.id: 0.3 for p in players},
    )

    profiles = compute_minutes_profiles(db_session)

    forwards = [profiles[p.id] for p in teams[0].players if p.element_type == FWD]
    assert all(f.source == "model" for f in forwards)
    # Three forwards at 0.3 each stay at 0.9 in total; the 4-4-2 prior would
    # have stretched them to 2.0.
    assert sum(f.p_start for f in forwards) == pytest.approx(0.9, abs=0.01)
