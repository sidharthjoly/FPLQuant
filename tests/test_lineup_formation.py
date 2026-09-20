import pytest
from sqlalchemy.orm import Session

from fplquant.lineup.formation import (
    DEFAULT_SLOTS,
    compute_team_shapes,
    describe_shape,
)
from fplquant.models.orm import Fixture
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


def _played_fixture(session: Session, home, away, *, fpl_id: int, event: int) -> None:
    session.add(
        Fixture(
            fpl_id=fpl_id,
            event=event,
            team_h_id=home.id,
            team_a_id=away.id,
            team_h_difficulty=3,
            team_a_difficulty=3,
            team_h_score=1,
            team_a_score=1,
            finished=True,
        )
    )
    session.flush()


def _start_an_xi(
    session: Session, team, shape: dict[int, int], *, fixture_fpl_id: int, at_home: bool
) -> None:
    """A full XI for one club in one match, recorded against that fixture."""
    fpl_id = team.fpl_id * 100_000 + fixture_fpl_id * 100
    for position, count in shape.items():
        for _ in range(count):
            player = make_player(session, team, fpl_id=fpl_id, element_type=position)
            fpl_id += 1
            stat = make_stat(session, player, round_number=1, minutes=90, starts=1)
            stat.fixture_fpl_id = fixture_fpl_id
            stat.was_home = at_home
    session.flush()


@pytest.mark.parametrize(
    ("slots", "expected"),
    [
        # Brentford, live: sums to exactly 10.00 and used to print "3-5-1".
        ({DEFENDER: 3.44, MIDFIELDER: 5.11, FORWARD: 1.44}, "3-5-2"),
        # Spurs, live: also sums to 10.00, used to print "4-5-2" — twelve men.
        # The two spare places go to the largest remainders, .78 and .67.
        ({DEFENDER: 3.78, MIDFIELDER: 4.56, FORWARD: 1.67}, "4-4-2"),
        # Python rounds half to even, so 2.5 formats as "2" and this read
        # "2-5-2" — nine players, and the shape that started this hunt.
        ({DEFENDER: 2.5, MIDFIELDER: 5.4, FORWARD: 2.1}, "3-5-2"),
        ({DEFENDER: 4.0, MIDFIELDER: 4.0, FORWARD: 2.0}, "4-4-2"),
    ],
)
def test_a_formation_always_fields_ten_outfield_players(
    slots: dict[int, float], expected: str
) -> None:
    """Ten is not a preference, it is how many outfield players a team may put
    on the pitch. Rounding each position on its own does not preserve it."""
    label = describe_shape(slots)

    assert label == expected
    assert sum(int(part) for part in label.split("-")) == 10


def test_a_transferred_players_old_matches_stay_with_his_old_club(
    db_session: Session,
) -> None:
    """A player's history follows him through a transfer. Attributing all of it
    to the club he is at now invented lineups: Man City's shape was partly
    computed from two fixtures in which exactly one Man City player appeared,
    which dragged their average XI below seven outfield players."""
    city = make_team(db_session, 1, "MCI")
    rovers = make_team(db_session, 2, "ROV")
    _played_fixture(db_session, city, rovers, fpl_id=500, event=1)

    # A full XI for Rovers in that match...
    _start_an_xi(
        db_session, rovers, {GKP: 1, DEF: 3, MID: 5, FWD: 2}, fixture_fpl_id=500, at_home=False
    )
    # ...one of whom has since signed for City, and brings his history along.
    moved = make_player(db_session, city, fpl_id=99, element_type=MID)
    stat = make_stat(db_session, moved, round_number=1, minutes=90, starts=1)
    stat.fixture_fpl_id = 500
    stat.was_home = False
    db_session.flush()

    shapes = {shape.short_name: shape for shape in compute_team_shapes(db_session)}

    # City never played in that fixture as far as the lineup data is concerned.
    assert shapes["MCI"].rounds_observed == 0
    assert shapes["MCI"].slots == DEFAULT_SLOTS


def test_a_match_that_is_not_a_full_xi_is_not_evidence_of_a_shape(
    db_session: Session,
) -> None:
    """A gameweek still in progress, or a club whose eleventh starter has left
    the league, gives a count that is not a lineup. Averaging it in moves the
    club toward a formation nobody played."""
    team = make_team(db_session, 1, "ARS")
    other = make_team(db_session, 2, "ROV")
    _played_fixture(db_session, team, other, fpl_id=501, event=1)
    _played_fixture(db_session, team, other, fpl_id=502, event=2)

    _start_an_xi(
        db_session, team, {GKP: 1, DEF: 3, MID: 5, FWD: 2}, fixture_fpl_id=501, at_home=True
    )
    # Half a lineup from the match still being played.
    _start_an_xi(db_session, team, {GKP: 1, DEF: 2}, fixture_fpl_id=502, at_home=True)

    shape = next(s for s in compute_team_shapes(db_session) if s.short_name == "ARS")

    assert shape.rounds_observed == 1


def test_every_clubs_shape_adds_up_to_a_legal_eleven(db_session: Session) -> None:
    """The invariant behind the label. If the slots themselves do not sum to
    ten, no amount of careful rounding makes the formation mean anything."""
    team = make_team(db_session, 1, "ARS")
    other = make_team(db_session, 2, "ROV")
    _played_fixture(db_session, team, other, fpl_id=503, event=1)
    _start_an_xi(
        db_session, team, {GKP: 1, DEF: 5, MID: 3, FWD: 2}, fixture_fpl_id=503, at_home=True
    )

    for shape in compute_team_shapes(db_session):
        outfield = sum(shape.slots[position] for position in (DEFENDER, MIDFIELDER, FORWARD))
        assert outfield == pytest.approx(10.0)
