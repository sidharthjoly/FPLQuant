import pytest
from sqlalchemy.orm import Session

from fplquant.lineup.formation import (
    DEFAULT_SLOTS,
    compute_team_shapes,
    describe_shape,
)
from fplquant.optimizer.types import DEFENDER, FORWARD, MIDFIELDER
from tests.lineup_helpers import DEF, FWD, GKP, MID, make_player, make_stat, make_team


def _start_a_shape(session: Session, team, shape: dict[int, int], *, rounds: int) -> None:
    """Start `shape[position]` players in each position, for `rounds` gameweeks."""
    fpl_id = team.fpl_id * 100
    for position, count in shape.items():
        for _ in range(count):
            player = make_player(session, team, fpl_id=fpl_id, element_type=position)
            fpl_id += 1
            for round_number in range(1, rounds + 1):
                make_stat(session, player, round_number=round_number, minutes=90, starts=1)


def test_a_side_with_no_history_is_assumed_to_play_the_prior_shape(db_session: Session) -> None:
    make_team(db_session, 1, "ARS")

    shape = compute_team_shapes(db_session)[0]

    assert shape.rounds_observed == 0
    assert shape.slots == DEFAULT_SLOTS
    assert describe_shape(shape.slots) == "4-4-2"


def test_a_settled_back_three_is_read_off_who_actually_starts(db_session: Session) -> None:
    team = make_team(db_session, 1, "ARS")
    _start_a_shape(db_session, team, {GKP: 1, DEF: 3, MID: 5, FWD: 2}, rounds=12)

    shape = compute_team_shapes(db_session)[0]

    assert shape.rounds_observed == 12
    assert shape.slots[DEFENDER] == pytest.approx(3.0, abs=0.3)
    assert shape.slots[MIDFIELDER] == pytest.approx(5.0, abs=0.3)
    assert describe_shape(shape.slots) == "3-5-2"


def test_one_round_of_evidence_barely_moves_the_shape(db_session: Session) -> None:
    """The GW2 problem again: a single match in a back three is not proof a club
    has switched systems, so the estimate stays close to the 4-4-2 prior."""
    team = make_team(db_session, 1, "ARS")
    _start_a_shape(db_session, team, {GKP: 1, DEF: 3, MID: 5, FWD: 2}, rounds=1)

    shape = compute_team_shapes(db_session)[0]

    # One round against a credibility of four: w = 1/5, so 3*0.2 + 4*0.8 = 3.8.
    assert shape.slots[DEFENDER] == pytest.approx(3.8)
    assert describe_shape(shape.slots) == "4-4-2"


def test_shapes_are_tracked_per_club(db_session: Session) -> None:
    back_three = make_team(db_session, 1, "ARS")
    back_five = make_team(db_session, 2, "CHE")
    _start_a_shape(db_session, back_three, {GKP: 1, DEF: 3, MID: 5, FWD: 2}, rounds=12)
    _start_a_shape(db_session, back_five, {GKP: 1, DEF: 5, MID: 4, FWD: 1}, rounds=12)

    by_name = {s.short_name: s for s in compute_team_shapes(db_session)}

    assert by_name["ARS"].slots[DEFENDER] < by_name["CHE"].slots[DEFENDER]
    assert by_name["CHE"].slots[FORWARD] < by_name["ARS"].slots[FORWARD]


def test_benched_players_do_not_count_toward_the_shape(db_session: Session) -> None:
    team = make_team(db_session, 1, "ARS")
    _start_a_shape(db_session, team, {GKP: 1, DEF: 3, MID: 5, FWD: 2}, rounds=12)
    # Two more defenders who are in the squad but never start.
    for fpl_id in (900, 901):
        benched = make_player(db_session, team, fpl_id=fpl_id, element_type=DEF)
        for round_number in range(1, 13):
            make_stat(db_session, benched, round_number=round_number, minutes=0, starts=0)

    shape = compute_team_shapes(db_session)[0]

    assert shape.slots[DEFENDER] == pytest.approx(3.0, abs=0.3)
