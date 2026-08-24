import pytest
from sqlalchemy.orm import Session

from fplquant.models.orm import Player, PlayerGameweekStat, Team
from fplquant.optimizer.candidates import build_candidates_from_db
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER, PlayerCandidate
from fplquant.transfers.planner import propose_transfers


def _player(
    player_id: int,
    element_type: int,
    points: float,
    cost: int = 50,
    team_id: int | None = None,
    web_name: str | None = None,
) -> PlayerCandidate:
    team_id = team_id if team_id is not None else player_id  # unique club per player by default
    return PlayerCandidate(
        player_id=player_id,
        web_name=web_name or f"P{player_id}",
        team_id=team_id,
        team_short_name=f"T{team_id}",
        element_type=element_type,
        now_cost=cost,
        predicted_points=points,
    )


def _base_squad() -> list[PlayerCandidate]:
    """A minimal valid 15-man squad: 2 GKP, 5 DEF, 5 MID, 3 FWD, all modest
    and equal points/cost, so there's clean room to test both upgrades and
    budget constraints without incidental interference."""
    players = []
    pid = 1
    for position, count in [(GOALKEEPER, 2), (DEFENDER, 5), (MIDFIELDER, 5), (FORWARD, 3)]:
        for _ in range(count):
            players.append(_player(pid, position, points=4.0, cost=50))
            pid += 1
    return players


def test_no_transfer_recommended_when_no_better_option_exists() -> None:
    squad = _base_squad()

    plan = propose_transfers(squad, all_candidates=squad, bank=0, free_transfers=1)

    assert plan.transfers_made == 0
    assert plan.worth_it is False
    assert plan.hit_cost == 0
    assert plan.transfers == []


def test_recommends_a_free_upgrade_within_free_transfers() -> None:
    squad = _base_squad()
    upgrade = _player(100, FORWARD, points=20.0, cost=50, team_id=100, web_name="Star")
    pool = squad + [upgrade]

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=1)

    assert plan.transfers_made == 1
    assert plan.transfers[0].in_.player_id == 100
    assert plan.hit_cost == 0
    assert plan.worth_it is True
    assert plan.points_gain_after_hit == pytest.approx(16.0)


def test_recommends_transfer_when_gain_exceeds_the_hit_cost() -> None:
    squad = _base_squad()
    upgrade = _player(100, FORWARD, points=20.0, cost=50, team_id=100, web_name="Star")
    pool = squad + [upgrade]

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=0)

    assert plan.transfers_made == 1
    assert plan.hit_cost == 4
    assert plan.points_gain_after_hit == pytest.approx(16.0 - 4.0)
    assert plan.worth_it is True


def test_does_not_recommend_transfer_when_gain_is_smaller_than_the_hit_cost() -> None:
    squad = _base_squad()
    # +2 points is a real upgrade, but not enough to outweigh the -4 hit.
    tiny_upgrade = _player(100, FORWARD, points=6.0, cost=50, team_id=100, web_name="SlightBetter")
    pool = squad + [tiny_upgrade]

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=0)

    assert plan.transfers_made == 0
    assert plan.hit_cost == 0
    assert plan.worth_it is False


def test_respects_budget_constraint() -> None:
    squad = _base_squad()  # every player costs 50, bank=0 -> no spare budget
    unaffordable_upgrade = _player(100, FORWARD, points=50.0, cost=200, team_id=100)
    pool = squad + [unaffordable_upgrade]

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=5)

    assert plan.transfers_made == 0


def test_respects_max_per_club_constraint() -> None:
    squad = _base_squad()
    same_club_upgrades = [
        _player(101, GOALKEEPER, points=10.0, cost=50, team_id=999, web_name="GK"),
        _player(102, DEFENDER, points=10.0, cost=50, team_id=999, web_name="DF"),
        _player(103, MIDFIELDER, points=10.0, cost=50, team_id=999, web_name="MF"),
        _player(104, FORWARD, points=10.0, cost=50, team_id=999, web_name="FW"),
    ]
    pool = squad + same_club_upgrades

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=10, max_per_club=3)

    club_999_count = sum(1 for p in plan.resulting_squad.players if p.team_id == 999)
    assert club_999_count <= 3


def test_wildcard_chip_ignores_free_transfers_and_hit_cost() -> None:
    squad = _base_squad()
    upgrades = [
        _player(100 + i, FORWARD if i < 3 else MIDFIELDER, points=20.0, cost=50, team_id=100 + i)
        for i in range(6)
    ]
    pool = squad + upgrades

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=0, chip="wildcard")

    assert plan.hit_cost == 0
    assert plan.transfers_made > 0
    assert plan.worth_it is True


def test_free_hit_chip_also_ignores_hit_cost() -> None:
    squad = _base_squad()
    upgrade = _player(100, FORWARD, points=20.0, cost=50, team_id=100)
    pool = squad + [upgrade]

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=0, chip="free_hit")

    assert plan.chip == "free_hit"
    assert plan.hit_cost == 0
    assert plan.transfers_made == 1


def test_transfer_pairs_match_by_position() -> None:
    squad = _base_squad()
    upgrades = [
        _player(101, FORWARD, points=20.0, cost=50, team_id=101, web_name="FwdUp"),
        _player(102, MIDFIELDER, points=20.0, cost=50, team_id=102, web_name="MidUp"),
    ]
    pool = squad + upgrades

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=2)

    assert plan.transfers_made == 2
    for pair in plan.transfers:
        assert pair.out.element_type == pair.in_.element_type


def test_current_squad_player_stays_selectable_even_if_excluded_from_candidates() -> None:
    squad = _base_squad()
    pool = squad[1:]  # squad[0] is owned but missing from the candidate pool

    plan = propose_transfers(squad, all_candidates=pool, bank=0, free_transfers=1)

    assert any(p.player_id == squad[0].player_id for p in plan.resulting_squad.players)


def test_resulting_squad_and_starting_xi_are_well_formed() -> None:
    squad = _base_squad()

    plan = propose_transfers(squad, all_candidates=squad, bank=0, free_transfers=1)

    assert len(plan.resulting_squad.players) == 15
    assert len(plan.starting_xi.starters) == 11
    assert len(plan.starting_xi.bench) == 4


def _seed_league_after_one_gameweek(session: Session) -> list[Player]:
    """A player pool with exactly one gameweek played, in which a handful of
    players hauled and everyone else returned a typical low score."""
    teams = []
    for team_id in range(1, 21):
        team = Team(fpl_id=team_id, name=f"Club {team_id}", short_name=f"C{team_id:02d}")
        session.add(team)
        teams.append(team)
    session.flush()

    players = []
    fpl_id = 1
    for team in teams:
        for position, count in [(GOALKEEPER, 2), (DEFENDER, 5), (MIDFIELDER, 5), (FORWARD, 3)]:
            for _ in range(count):
                player = Player(
                    fpl_id=fpl_id,
                    team_id=team.id,
                    first_name="P",
                    second_name=str(fpl_id),
                    web_name=f"P{fpl_id}",
                    element_type=position,
                    now_cost=50,
                    ep_next=4.0,
                    status="a",
                )
                session.add(player)
                players.append(player)
                fpl_id += 1
    session.flush()

    # Every 20th player hauled 15 in GW1; everyone else scored an ordinary 2.
    for index, player in enumerate(players):
        session.add(
            PlayerGameweekStat(
                player_id=player.id,
                round=1,
                minutes=90,
                total_points=15 if index % 20 == 0 else 2,
            )
        )
    session.flush()
    return players


def test_one_gameweek_of_history_does_not_trigger_a_wholesale_rebuild(
    db_session: Session,
) -> None:
    """The GW2 blow-up: with a single gameweek on record, a player's raw EWMA
    form *is* that gameweek's score, so last week's team of the week looks
    enormously better than anyone else and the solver happily takes a -56 hit
    to buy all of them. Shrinking form toward the prior keeps one week of
    evidence in proportion.
    """
    _seed_league_after_one_gameweek(db_session)
    candidates = build_candidates_from_db(db_session)
    by_id = {c.player_id: c for c in candidates}

    # A squad of players who all blanked in GW1 — the worst realistic case for
    # churn, since every haul in the pool is a candidate upgrade. Built
    # respecting the max-3-per-club rule, so any transfer the solver proposes
    # is one it actually wants, not one forced by an illegal starting squad.
    blanked = [c for c in candidates if by_id[c.player_id].predicted_points < 4.0]
    squad: list[PlayerCandidate] = []
    per_club: dict[int, int] = {}
    for position, count in [(GOALKEEPER, 2), (DEFENDER, 5), (MIDFIELDER, 5), (FORWARD, 3)]:
        taken = 0
        for c in blanked:
            if taken == count:
                break
            if c.element_type != position or per_club.get(c.team_id, 0) >= 3:
                continue
            squad.append(c)
            per_club[c.team_id] = per_club.get(c.team_id, 0) + 1
            taken += 1
        assert taken == count, f"could not fill {count} of position {position}"

    plan = propose_transfers(squad, candidates, bank=0, free_transfers=1)

    assert plan.transfers_made <= 2, (
        f"one gameweek of data triggered {plan.transfers_made} transfers "
        f"for a {plan.hit_cost}-point hit"
    )
    assert plan.hit_cost <= 4
