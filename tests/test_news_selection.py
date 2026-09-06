"""The news read as a statement about selection rather than about fitness.

See `fplquant.news.selection` for why the two come apart: FPL's percentage
answers "will he play", and the engine spends it answering "will he start".
"""

import pytest
from sqlalchemy.orm import Session

from fplquant.config import settings
from fplquant.engine.minutes import MinutesProfile, compute_minutes_profiles
from fplquant.form.fixtures import chance_of_playing
from fplquant.news.selection import start_gate
from tests.engine_helpers import FWD, GKP, make_league, make_player, make_team


def test_a_fit_player_and_an_absent_one_are_both_untouched() -> None:
    """The transform is inert at both ends, which is what makes it safe to run
    over the whole pool: the ~97% of players with nothing wrong with them come
    through bit for bit, and nobody is pushed below zero."""
    assert start_gate(1.0) == 1.0
    assert start_gate(0.0) == 0.0


def test_a_doubt_costs_more_start_probability_than_it_costs_availability() -> None:
    """The whole point. A manager being careful with a player benches him more
    often than he leaves him out, so 75% to feature is well under 75% to start."""
    for availability in (0.25, 0.5, 0.75, 0.9):
        assert start_gate(availability) < availability


def test_the_gate_never_reorders_two_players() -> None:
    """Monotone by construction, so it can only ever scale the ordering the
    availability number already established — never invert it."""
    values = [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]
    gated = [start_gate(v) for v in values]
    assert gated == sorted(gated)


def test_the_kill_switch_hands_back_the_bare_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "news_selection_feeds_the_model", False)
    for availability in (0.25, 0.5, 0.75):
        assert start_gate(availability) == availability


def test_a_doubtful_players_slot_goes_to_his_teammates(db_session: Session) -> None:
    """The heuristic arm. `_normalise_to_slots` rescales the group back up to
    the shirts its formation fills, so being careful with one player promotes
    whoever is behind him rather than leaving the club a man short."""
    team = make_team(db_session, fpl_id=1, short_name="AAA")
    first_choice = make_player(db_session, team, fpl_id=1, element_type=GKP, now_cost=55)
    understudy = make_player(db_session, team, fpl_id=2, element_type=GKP, now_cost=40)

    healthy = compute_minutes_profiles(db_session)

    first_choice.status = "d"
    first_choice.chance_of_playing_next_round = 75
    db_session.flush()
    doubtful = compute_minutes_profiles(db_session)

    assert doubtful[first_choice.id].p_start < healthy[first_choice.id].p_start
    assert doubtful[understudy.id].p_start > healthy[understudy.id].p_start
    # One keeper starts either way — the club still names a goalkeeper.
    assert doubtful[first_choice.id].p_start + doubtful[understudy.id].p_start == pytest.approx(
        1.0, abs=0.01
    )


def test_the_model_arm_hands_the_freed_mass_over_rather_than_deleting_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model calibrates a group's total itself, so the discount has to go
    through `_redistribute_unavailable`'s gate vector — which frees exactly the
    mass it withholds — rather than into the estimate, which would quietly
    leave the position group short of a starter."""

    class _StubModel:
        def predict(self, rows: list[object]) -> list[float]:
            return [0.3] * len(rows)

    teams = make_league(db_session, teams=2)
    monkeypatch.setattr("fplquant.engine.minutes.load", lambda *a, **k: _StubModel())
    monkeypatch.setattr(
        "fplquant.engine.minutes._model_probabilities",
        lambda session, players, trained: {p.id: 0.3 for p in players},
    )

    forwards = [p for p in teams[0].players if p.element_type == FWD]
    forwards[0].status = "d"
    forwards[0].chance_of_playing_next_round = 75
    db_session.flush()

    profiles = compute_minutes_profiles(db_session)

    assert all(profiles[f.id].source == "model" for f in forwards)
    assert profiles[forwards[0].id].p_start < 0.3  # the doubtful one gives mass up
    assert all(profiles[f.id].p_start > 0.3 for f in forwards[1:])  # his teammates take it
    # Three forwards the model put at 0.3 still total 0.9 between them.
    assert sum(profiles[f.id].p_start for f in forwards) == pytest.approx(0.9, abs=0.01)


def test_the_reported_availability_is_still_fpls_own_number(db_session: Session) -> None:
    """`MinutesProfile.availability` means fitness and has to stay bit-identical
    to what FPL published — the contract the whole news layer is pinned to. The
    selection discount lives in the start probability, not in this field."""
    team = make_team(db_session, fpl_id=1, short_name="AAA")
    player = make_player(db_session, team, fpl_id=1, status="d", chance_of_playing_next_round=75)

    profile = compute_minutes_profiles(db_session)[player.id]

    assert profile.availability == chance_of_playing(player) == 0.75


def test_a_doubtful_player_is_no_less_likely_to_appear_off_the_bench(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bench term reads the bare fitness gate on purpose. Being carefully
    handled is the *reason* a player ends up a substitute, so discounting his
    bench odds by the same signal would double-count it in the wrong direction."""
    team = make_team(db_session, fpl_id=1, short_name="AAA")
    # Two keepers for one shirt, so the normalisation has somewhere to move the
    # freed mass to and the discount is not swallowed by the certainty cap.
    player = make_player(
        db_session,
        team,
        fpl_id=1,
        element_type=GKP,
        now_cost=55,
        status="d",
        chance_of_playing_next_round=75,
    )
    make_player(db_session, team, fpl_id=2, element_type=GKP, now_cost=40)

    with_signal = compute_minutes_profiles(db_session)[player.id]
    monkeypatch.setattr(settings, "news_selection_feeds_the_model", False)
    without_signal = compute_minutes_profiles(db_session)[player.id]

    assert with_signal.p_bench_appearance == without_signal.p_bench_appearance
    assert with_signal.p_start < without_signal.p_start


def test_the_discount_never_manufactures_appearance_mass(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving a player from the XI to the bench must not make him *likelier* to
    feature. Start probability falls while the bench term is deliberately left
    on the bare fitness gate, so the composite has to be checked rather than
    assumed: leaving the bench term alone is meant to keep it a floor, not to
    hand the player a second helping.

    Asserted on the model arm, which is what production runs and where
    `_redistribute_unavailable` keeps the group at the total the model chose.
    The heuristic arm's `_normalise_to_slots` scales a gated player back up to
    fill his formation's shirts — pre-existing and deliberate, since somebody
    has to keep goal — so the absolute bound below is not available there.
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
    player = next(p for p in teams[0].players if p.element_type == FWD)
    player.status = "d"
    player.chance_of_playing_next_round = 75
    db_session.flush()

    def appearance(profile: MinutesProfile) -> float:
        return profile.p_start + (1 - profile.p_start) * profile.p_bench_appearance

    with_signal = compute_minutes_profiles(db_session)[player.id]
    monkeypatch.setattr(settings, "news_selection_feeds_the_model", False)
    without_signal = compute_minutes_profiles(db_session)[player.id]

    assert with_signal.source == "model"
    assert appearance(with_signal) < appearance(without_signal)
    assert appearance(with_signal) <= with_signal.availability
    assert with_signal.expected_minutes < without_signal.expected_minutes
