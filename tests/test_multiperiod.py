import pytest

from fplquant.optimizer.multiperiod import (
    AVAILABLE_CHIPS,
    BENCH_BOOST,
    FREE_HIT,
    MAX_FREE_TRANSFERS,
    TRIPLE_CAPTAIN,
    WILDCARD,
    HorizonCandidate,
    plan_horizon,
)
from fplquant.optimizer.types import (
    DEFENDER,
    FORWARD,
    GOALKEEPER,
    MIDFIELDER,
    InfeasibleSquadError,
    PlayerCandidate,
)

# Enough of a pool to fill a legal squad several times over, spread across
# clubs so the three-per-club limit never binds by accident.
POOL: list[tuple[int, int]] = (
    [(GOALKEEPER, 6)] + [(DEFENDER, 12)] + [(MIDFIELDER, 12)] + [(FORWARD, 9)]
)


# Ids are handed out in pool order, so these ranges name positions.
GOALKEEPER_IDS = list(range(1, 7))
DEFENDER_IDS = list(range(7, 19))
MIDFIELDER_IDS = list(range(19, 31))
FORWARD_IDS = list(range(31, 40))

INCUMBENTS = set(GOALKEEPER_IDS[:2] + DEFENDER_IDS[:5] + MIDFIELDER_IDS[:5] + FORWARD_IDS[:3])
CHALLENGERS = set(
    GOALKEEPER_IDS[2:4] + DEFENDER_IDS[5:10] + MIDFIELDER_IDS[5:10] + FORWARD_IDS[3:6]
)


def _pool(
    events: list[int],
    points: dict[int, dict[int, float]] | None = None,
    cost: int = 50,
) -> list[HorizonCandidate]:
    """A generic candidate pool. `points` overrides specific players' scores by
    id; everyone else is worth a flat, identical amount, so any difference the
    solver produces has to come from the override."""
    points = points or {}
    candidates = []
    player_id = 0
    for element_type, count in POOL:
        for index in range(count):
            player_id += 1
            candidates.append(
                HorizonCandidate(
                    candidate=PlayerCandidate(
                        player_id=player_id,
                        web_name=f"P{player_id}",
                        # One club each: the three-per-club limit is exercised
                        # by the single-gameweek optimizer's tests, and letting
                        # it bind here would confound every other assertion.
                        team_id=player_id,
                        team_short_name=f"T{player_id}",
                        element_type=element_type,
                        now_cost=cost,
                        predicted_points=1.0,
                    ),
                    points_by_event=points.get(
                        player_id, dict.fromkeys(events, 1.0 + index * 0.001)
                    ),
                )
            )
    return candidates


def test_a_squad_that_is_kept_costs_no_transfers() -> None:
    """The flow constraint is what turns a sequence of independent picks into a
    plan: this week's squad is last week's, plus what you bought, minus what
    you sold."""
    events = [1, 2, 3]
    candidates = _pool(events, points={pid: dict.fromkeys(events, 5.0) for pid in INCUMBENTS})

    plan = plan_horizon(candidates, events, budget=1000, current_squad_ids=INCUMBENTS)

    assert plan.gameweeks[0].transfers_in == []
    assert plan.gameweeks[0].transfers_out == []
    assert plan.total_hit_cost == 0


def test_it_banks_a_transfer_when_there_is_nothing_worth_buying() -> None:
    """A myopic solver cannot value a free transfer, because a free transfer is
    worth exactly the flexibility it gives you later, and later is not in its
    model."""
    events = [1, 2, 3]
    candidates = _pool(events, points={pid: dict.fromkeys(events, 5.0) for pid in INCUMBENTS})

    plan = plan_horizon(
        candidates, events, budget=1000, current_squad_ids=INCUMBENTS, free_transfers=1
    )

    assert plan.gameweeks[0].transfers_in == []
    assert plan.gameweeks[1].free_transfers_available == 2


def test_banked_transfers_stop_at_the_cap() -> None:
    events = [1, 2, 3, 4, 5, 6, 7]
    candidates = _pool(events, points={pid: dict.fromkeys(events, 5.0) for pid in INCUMBENTS})

    plan = plan_horizon(candidates, events, budget=1000, current_squad_ids=INCUMBENTS)

    assert all(
        gameweek.free_transfers_available <= MAX_FREE_TRANSFERS for gameweek in plan.gameweeks
    )


def test_a_hit_is_taken_only_when_it_pays_for_itself() -> None:
    """Two identical setups differing only in how much better the alternative
    is. The -4 has to be the thing that decides it, which is the question
    "is it worth a hit" answered by construction rather than as an afterthought."""
    events = [1, 2]
    target = min(CHALLENGERS)

    def solve(target_points: float) -> float:
        candidates = _pool(events, points={target: dict.fromkeys(events, target_points)})
        return plan_horizon(
            candidates,
            events,
            budget=1000,
            current_squad_ids=INCUMBENTS,
            free_transfers=0,
        ).total_hit_cost

    assert solve(1.5) == 0
    assert solve(30.0) > 0


def test_the_triple_captain_lands_on_the_best_week_for_it() -> None:
    """Chip timing is a question no single-gameweek model can even ask: knowing
    which week your captain hauls requires looking at all of them together."""
    events = [1, 2, 3]
    spike = {1: 5.0, 2: 40.0, 3: 5.0}
    candidates = _pool(events, points={31: spike})  # a forward, so he can start

    plan = plan_horizon(candidates, events, budget=1000, chips=frozenset({TRIPLE_CAPTAIN}))

    played = [gameweek.event for gameweek in plan.gameweeks if gameweek.chip == TRIPLE_CAPTAIN]
    assert played == [2]


def test_each_chip_is_played_at_most_once_within_a_half_season() -> None:
    events = [1, 2, 3, 4]
    candidates = _pool(events)

    plan = plan_horizon(
        candidates,
        events,
        budget=1000,
        chips=frozenset({BENCH_BOOST, TRIPLE_CAPTAIN}),
    )

    for chip in (BENCH_BOOST, TRIPLE_CAPTAIN):
        assert sum(1 for gameweek in plan.gameweeks if gameweek.chip == chip) <= 1


def test_a_horizon_crossing_the_halfway_point_gets_both_chip_sets() -> None:
    """FPL issues two full sets of chips, one for gameweeks 1-19 and one for
    20-38. A plan spanning the boundary that allows only one is leaving a chip
    on the table."""
    events = [18, 19, 20, 21]
    spike = {18: 1.0, 19: 40.0, 20: 40.0, 21: 1.0}
    hauls = dict.fromkeys(list(INCUMBENTS)[:3], spike)
    candidates = _pool(events, points=hauls)

    plan = plan_horizon(candidates, events, budget=1000, chips=frozenset({TRIPLE_CAPTAIN}))

    played = [gameweek.event for gameweek in plan.gameweeks if gameweek.chip == TRIPLE_CAPTAIN]
    assert len(played) == 2, "one chip per half-season, and this horizon spans both"
    assert min(played) < 20 <= max(played)


def test_a_free_hit_reverts_the_squad_the_following_week() -> None:
    """Without the reversion constraint a free hit is a strictly better
    wildcard — unlimited transfers *and* you keep the squad — so the solver
    plays it every time and the plan it returns is not one you could play."""
    events = [1, 2, 3]
    # A one-week spike: worth buying into for GW2 alone, not worth keeping.
    spike = {player_id: {1: 1.0, 2: 30.0, 3: 1.0} for player_id in CHALLENGERS}
    candidates = _pool(events, points=spike)

    plan = plan_horizon(
        candidates,
        events,
        budget=1000,
        current_squad_ids=INCUMBENTS,
        free_transfers=1,
        chips=frozenset({FREE_HIT}),
    )

    by_event = {gameweek.event: gameweek for gameweek in plan.gameweeks}
    free_hit_weeks = [e for e, gw in by_event.items() if gw.chip == FREE_HIT]
    assert free_hit_weeks == [2]
    assert by_event[2].hit_cost == 0
    # The squad in GW3 is the one owned in GW1, not the one the chip bought.
    reverted = {p.player_id for p in by_event[3].squad.players}
    before = {p.player_id for p in by_event[1].squad.players}
    assert reverted == before


def test_a_free_hit_is_never_scheduled_in_the_final_gameweek() -> None:
    """Its whole cost falls in the week the squad reverts, and that week is
    outside the horizon — so the solver would see free transfers at no price."""
    events = [1, 2]
    spike = {player_id: {1: 1.0, 2: 30.0} for player_id in CHALLENGERS}
    candidates = _pool(events, points=spike)

    plan = plan_horizon(
        candidates,
        events,
        budget=1000,
        current_squad_ids=INCUMBENTS,
        chips=frozenset({FREE_HIT}),
    )

    assert all(gameweek.chip != FREE_HIT for gameweek in plan.gameweeks)


def test_a_wildcard_waives_the_hits_for_its_week() -> None:
    """A whole squad's worth of transfers costs 56 points of hits without the
    chip, and nothing with it."""
    events = [1, 2]
    upgrades = {player_id: dict.fromkeys(events, 20.0) for player_id in CHALLENGERS}
    candidates = _pool(events, points=upgrades)

    plan = plan_horizon(
        candidates,
        events,
        budget=1000,
        current_squad_ids=INCUMBENTS,
        free_transfers=1,
        chips=frozenset({WILDCARD}),
    )

    wildcard_weeks = [gameweek for gameweek in plan.gameweeks if gameweek.chip == WILDCARD]
    assert wildcard_weeks
    assert wildcard_weeks[0].hit_cost == 0
    assert len(wildcard_weeks[0].transfers_in) > 1


def test_the_starting_xi_is_chosen_inside_the_optimization() -> None:
    """Maximizing over all fifteen values a fourth goalkeeper the same as a
    first-choice striker. Maximizing over the eleven that score puts the money
    where the points are."""
    events = [1]
    candidates = _pool(events)

    plan = plan_horizon(candidates, events, budget=1000)

    xi = plan.gameweeks[0].starting_xi
    assert len(xi.starters) == 11
    assert len(xi.bench) == 4
    assert sum(1 for p in xi.starters if p.element_type == GOALKEEPER) == 1
    assert plan.gameweeks[0].expected_points == pytest.approx(
        xi.starting_predicted_points + xi.captain.predicted_points
    )


def test_owning_a_player_outside_the_pool_is_an_error() -> None:
    """Silently dropping them would have the program "solve" by selling a squad
    it was never allowed to hold."""
    events = [1]
    with pytest.raises(InfeasibleSquadError, match="missing from the candidate pool"):
        plan_horizon(_pool(events), events, budget=1000, current_squad_ids={9999})


def test_an_unaffordable_squad_is_infeasible() -> None:
    events = [1]
    with pytest.raises(InfeasibleSquadError):
        plan_horizon(_pool(events, cost=200), events, budget=100)


def test_planning_over_nothing_is_an_error() -> None:
    with pytest.raises(InfeasibleSquadError):
        plan_horizon(_pool([1]), [], budget=1000)
    with pytest.raises(InfeasibleSquadError):
        plan_horizon([], [1], budget=1000)


def test_an_unknown_chip_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown chips"):
        plan_horizon(_pool([1]), [1], budget=1000, chips=frozenset({"assistant_manager"}))


def test_only_one_chip_can_be_played_in_a_gameweek() -> None:
    """FPL's rule, and without it the solver games the interaction: a wildcard
    makes a whole squad's transfers free, a bench boost then scores the bench it
    just bought, and all three pile into whichever single week has the best
    fixtures — producing a plan worth far more than any you are allowed to play.

    Asserted on `chips_played` rather than on `chip`, because `chip` reports
    only the first and would show a stacked week as an ordinary one-chip week.
    """
    events = [1, 2, 3]
    spike = {1: 2.0, 2: 60.0, 3: 2.0}
    candidates = _pool(events, points=dict.fromkeys(CHALLENGERS, spike))

    plan = plan_horizon(
        candidates,
        events,
        budget=1000,
        current_squad_ids=INCUMBENTS,
        free_transfers=1,
        chips=AVAILABLE_CHIPS,
    )

    for gameweek in plan.gameweeks:
        assert (
            len(gameweek.chips_played) <= 1
        ), f"GW{gameweek.event} plays {gameweek.chips_played} together"
