"""Multi-period squad planning: what to do over the next several gameweeks.

`fplquant.optimizer.squad` picks the best squad for one match, and
`fplquant.transfers.planner` picks the best transfer for one match. Both are
myopic in the way that costs FPL managers the most points. A single-gameweek
solver will happily take a -4 hit for a player with one good fixture, sell him
the week after for another -4, and never notice that banking the free transfer
for a fortnight would have got the same squad for nothing. It cannot value a
free transfer, because a free transfer is worth precisely the flexibility it
gives you *later*, and later is not in its model.

This module solves the whole horizon at once as a single integer program.
Squad membership, the starting XI, the captain, the transfers, the hits and the
free-transfer balance are all decision variables indexed by gameweek, tied
together by a flow constraint — this week's squad is last week's squad, plus
what you bought, minus what you sold. That one constraint is what turns a
sequence of independent picks into a plan.

Because the horizon is in the model, so are the things that only exist across
gameweeks: banking transfers, taking a hit now to avoid two later, buying into
a double gameweek three weeks early, and choosing which week to play a chip.
Chips are opt-in — each one adds variables, and a solve is cheaper without them.

The formulation is deliberately explicit about its own approximations. Prices
are held constant across the horizon, so it cannot plan around price rises;
selling fees are not modelled; and it assumes you keep the plan, when in
practice you re-solve every week and only ever execute the first move. That
last one is not a flaw. Re-solving with fresh information and executing only
the first step of the plan is exactly how model predictive control is meant to
be used, and it is why the discount on later gameweeks belongs in the
objective.
"""

import logging
from dataclasses import dataclass, replace

import pulp

from fplquant.optimizer.starting_xi import select_starting_xi
from fplquant.optimizer.types import (
    DEFENDER,
    FORWARD,
    GOALKEEPER,
    MIDFIELDER,
    InfeasibleSquadError,
    OptimizedSquad,
    PlayerCandidate,
    SquadConstraints,
    StartingXI,
)

logger = logging.getLogger(__name__)

TRANSFER_HIT_COST = 4
# FPL lets you bank unused transfers up to this many. Banking is the whole
# reason a horizon model beats a myopic one, so the cap is a real constraint.
MAX_FREE_TRANSFERS = 5

# What a banked free transfer is worth in the objective. It has to be positive,
# or the solver is indifferent between ending the horizon with five transfers
# in hand and ending with one — the value of a transfer is entirely in the
# gameweeks past the horizon, which by construction aren't modelled. It has to
# be well under the 4-point hit, or the plan hoards transfers it should spend.
#
# It is charged on every gameweek including the last, where strictly there is
# nothing left to spend it on. That's deliberate: the terminal balance is
# exactly the case the constant exists to price, since the horizon's edge is
# arbitrary and the season continues past it. The cost is a mild reluctance to
# transfer in the final modelled week, which is the week you were going to
# re-solve anyway.
FREE_TRANSFER_VALUE = 0.4

# Bench players score when a starter doesn't play, so a bench is worth
# something — but only a fraction, and paying full price for one is how a
# manager ends up with £20m sitting on it.
DEFAULT_BENCH_WEIGHT = 0.12

# Breaks ties toward not churning the squad, the same guard
# `fplquant.transfers.planner` uses.
_CHURN_EPSILON = 0.01

# Big-M for relaxing the hit constraint under a wildcard. Fifteen transfers is
# a whole squad, so nothing legal can exceed it.
_MAX_TRANSFERS = 15

FORMATION_LIMITS: dict[int, tuple[int, int]] = {
    GOALKEEPER: (1, 1),
    DEFENDER: (3, 5),
    MIDFIELDER: (2, 5),
    FORWARD: (1, 3),
}
STARTING_XI_SIZE = 11

WILDCARD = "wildcard"
BENCH_BOOST = "bench_boost"
TRIPLE_CAPTAIN = "triple_captain"
FREE_HIT = "free_hit"
AVAILABLE_CHIPS = frozenset({WILDCARD, BENCH_BOOST, TRIPLE_CAPTAIN, FREE_HIT})

# Chips whose transfers are free and which leave the free-transfer balance
# untouched. The wildcard keeps its new squad; the free hit gives it back.
_UNLIMITED_TRANSFER_CHIPS = (WILDCARD, FREE_HIT)

# FPL issues two full sets of chips a season, one usable in gameweeks 1-19 and
# one in 20-38, so a horizon that crosses the boundary may play each chip
# twice. A chip named in `chips` is taken to be available in every half the
# horizon touches; a manager who has already spent their first-half wildcard
# should simply plan from a gameweek after which that is no longer true.
CHIP_SECOND_HALF_FIRST_EVENT = 20


@dataclass(frozen=True)
class HorizonCandidate:
    """A player the planner may pick, with a points estimate per gameweek.

    `points_by_event` is where doubles and blanks enter the optimizer: a
    double gameweek is simply a larger number for that event, and a blank is a
    zero. Neither needs any special handling in the model.

    Note that `candidate.predicted_points` is the *horizon aggregate*, not any
    one gameweek's total — the planner never reads it, and the per-gameweek
    views it hands back have it replaced with that week's number.
    """

    candidate: PlayerCandidate
    points_by_event: dict[int, float]

    @property
    def player_id(self) -> int:
        return self.candidate.player_id

    def points(self, event: int) -> float:
        return self.points_by_event.get(event, 0.0)


@dataclass(frozen=True)
class GameweekPlan:
    event: int
    squad: OptimizedSquad
    starting_xi: StartingXI
    transfers_in: list[PlayerCandidate]
    transfers_out: list[PlayerCandidate]
    free_transfers_available: int
    hits_taken: int
    hit_cost: int
    # Every chip the solver switched on this week. FPL allows one, and the
    # program constrains it to one — but this records what actually came back
    # rather than what should have, because a single nullable field silently
    # truncates a violation into something that looks like a valid plan, and
    # that is exactly how the stacking bug survived its first round of tests.
    chips_played: list[str]
    expected_points: float  # starting XI plus captaincy, net of hits and chips

    @property
    def chip(self) -> str | None:
        """The chip played this week, if any — what every caller actually wants."""
        return self.chips_played[0] if self.chips_played else None


@dataclass(frozen=True)
class MultiPeriodPlan:
    gameweeks: list[GameweekPlan]
    total_expected_points: float
    total_hit_cost: int
    objective_value: float  # the discounted objective the solver actually maximized
    solver_status: str


def plan_horizon(
    candidates: list[HorizonCandidate],
    events: list[int],
    budget: int,
    current_squad_ids: set[int] | None = None,
    free_transfers: int = 1,
    constraints: SquadConstraints | None = None,
    decay: float = 0.9,
    chips: frozenset[str] = frozenset(),
    bench_weight: float = DEFAULT_BENCH_WEIGHT,
    solver_time_limit: int = 120,
) -> MultiPeriodPlan:
    """Solve the whole horizon as one integer program.

    `current_squad_ids` is the squad you own going into the first event. Pass
    None to build from scratch — the first gameweek's selection is then free,
    which is the preseason case and also what a wildcard week looks like.

    `chips` opts into scheduling chips. The solver places each at most once
    across the horizon, wherever it is worth most, which is a question no
    single-gameweek model can even ask: a triple captain is worth playing in
    the week your captain has a double gameweek, and knowing which week that is
    requires looking at all of them together.
    """
    if not candidates:
        raise InfeasibleSquadError("No candidate players supplied")
    if not events:
        raise InfeasibleSquadError("No gameweeks to plan over")
    unknown = chips - AVAILABLE_CHIPS
    if unknown:
        raise ValueError(f"Unknown chips: {sorted(unknown)}")

    constraints = constraints or SquadConstraints()
    by_id = {c.player_id: c for c in candidates}
    owned = set(current_squad_ids or set())
    missing = owned - set(by_id)
    if missing:
        raise InfeasibleSquadError(
            f"{len(missing)} owned player(s) are missing from the candidate pool"
        )

    problem = pulp.LpProblem("fpl_multiperiod_plan", pulp.LpMaximize)
    ids = list(by_id)

    squad = _binaries("squad", ids, events)
    start = _binaries("start", ids, events)
    captain = _binaries("captain", ids, events)
    buy = _binaries("buy", ids, events)
    sell = _binaries("sell", ids, events)

    free_balance = {
        event: pulp.LpVariable(
            f"free_{event}", lowBound=1, upBound=MAX_FREE_TRANSFERS, cat="Integer"
        )
        for event in events
    }
    hits = {event: pulp.LpVariable(f"hits_{event}", lowBound=0, cat="Integer") for event in events}
    # Free transfers actually consumed. A separate variable rather than the
    # expression `transfers - hits` because a wildcard spends no free transfers
    # at all — FPL preserves your balance through one — and the balance has a
    # floor of 1, so an expression that charged a wildcard's fifteen transfers
    # against it would make that week infeasible and force the hits straight
    # back, quietly making the chip worthless.
    used_free = {
        event: pulp.LpVariable(f"used_free_{event}", lowBound=0, cat="Integer") for event in events
    }

    objective = []
    for index, event in enumerate(events):
        weight = decay**index
        for player_id in ids:
            points = by_id[player_id].points(event)
            # A starter scores once, the captain scores again on top, and a
            # benched player is credited a fraction for the autosubs they win.
            objective.append(weight * points * start[player_id, event])
            objective.append(weight * points * captain[player_id, event])
            objective.append(
                weight * points * bench_weight * (squad[player_id, event] - start[player_id, event])
            )
        objective.append(-weight * TRANSFER_HIT_COST * hits[event])
        objective.append(weight * FREE_TRANSFER_VALUE * free_balance[event])
        objective.append(-_CHURN_EPSILON * pulp.lpSum(buy[player_id, event] for player_id in ids))

    chip_plays = _add_chips(
        problem, objective, chips, ids, events, by_id, squad, start, captain, decay, bench_weight
    )

    problem += pulp.lpSum(objective)

    for index, event in enumerate(events):
        _add_squad_constraints(problem, ids, by_id, event, squad, constraints, budget)
        _add_starting_xi_constraints(problem, ids, by_id, event, squad, start, captain)

        transfers = pulp.lpSum(buy[player_id, event] for player_id in ids)

        if index == 0 and current_squad_ids is None:
            # A free build: nothing is owned, so nothing is bought or sold and
            # the first squad costs no transfers.
            for player_id in ids:
                problem += buy[player_id, event] == 0
                problem += sell[player_id, event] == 0
        else:
            previous = (
                {pid: (1 if pid in owned else 0) for pid in ids}
                if index == 0
                else {pid: squad[pid, events[index - 1]] for pid in ids}
            )
            for player_id in ids:
                problem += (
                    squad[player_id, event]
                    == previous[player_id] + buy[player_id, event] - sell[player_id, event]
                )
                # Buying and selling the same player in the same week is a
                # no-op that the flow constraint alone would permit.
                problem += buy[player_id, event] + sell[player_id, event] <= 1

        # Hits are the transfers beyond the free ones. A wildcard or free hit
        # relaxes it: both make a whole squad's worth of transfers free.
        relaxation = _MAX_TRANSFERS * pulp.lpSum(
            chip_plays[chip][event] for chip in _UNLIMITED_TRANSFER_CHIPS if chip in chips
        )
        available = free_transfers if index == 0 else free_balance[events[index - 1]]
        problem += hits[event] >= transfers - available - relaxation

        # Next week's balance: what you didn't spend, plus one, capped. The
        # solver wants `free_balance` as high as the constraints allow because
        # of its small positive objective weight, so `<=` binds as equality —
        # and wants `used_free` as low as allowed, which pins it to the
        # transfers that weren't taken as hits, or to zero under a wildcard.
        problem += used_free[event] >= transfers - hits[event] - relaxation
        problem += free_balance[event] <= available - used_free[event] + 1

        # A free hit is a loan, not a wildcard: the squad it buys lasts one
        # gameweek and then reverts to whatever was owned before. Without this
        # the chip is a strictly better wildcard — free transfers *and* you
        # keep the squad — and the solver plays it every time.
        #
        # Expressed as a two-sided inequality that only binds when the chip is
        # played. Both sides are binaries, so their difference lies in [-1, 1]
        # and a big-M of 1 is enough.
        if FREE_HIT in chips and index + 1 < len(events):
            before = (
                {pid: (1 if pid in owned else 0) for pid in ids}
                if index == 0
                else {pid: squad[pid, events[index - 1]] for pid in ids}
            )
            after = events[index + 1]
            for player_id in ids:
                gap = squad[player_id, after] - before[player_id]
                problem += gap <= 1 - chip_plays[FREE_HIT][event]
                problem += -gap <= 1 - chip_plays[FREE_HIT][event]

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=solver_time_limit)
    problem.solve(solver)
    status = pulp.LpStatus[problem.status]
    if status != "Optimal":
        # CBC reports anything short of a *proven* optimum as not solved,
        # including a perfectly good incumbent it simply ran out of time to
        # prove. Discarding that would make a long horizon fail rather than
        # return the best plan found, which is the wrong trade for a planner
        # that gets re-run every week anyway.
        #
        # A genuinely infeasible program is not that case, and it does not
        # announce itself by leaving the variables unset — CBC leaves the LP
        # relaxation's values behind, which look like a solution and are not
        # one. So the squad is checked against the constraint that makes it a
        # squad, and only a time-limited run with a legal squad is accepted.
        if status in ("Infeasible", "Unbounded") or not _has_legal_squad(
            squad, ids, events, constraints
        ):
            raise InfeasibleSquadError(f"No feasible plan found (solver status: {status})")
        logger.warning(
            "Multi-period solve returned status=%s within %ss; using the best plan found",
            status,
            solver_time_limit,
        )

    return _extract_plan(
        problem,
        by_id,
        events,
        squad,
        start,
        captain,
        buy,
        sell,
        free_balance,
        hits,
        chip_plays,
        chips,
        free_transfers,
        decay,
    )


def _binaries(
    name: str, ids: list[int], events: list[int]
) -> dict[tuple[int, int], pulp.LpVariable]:
    return {
        (player_id, event): pulp.LpVariable(f"{name}_{player_id}_{event}", cat="Binary")
        for player_id in ids
        for event in events
    }


def _add_squad_constraints(
    problem: pulp.LpProblem,
    ids: list[int],
    by_id: dict[int, HorizonCandidate],
    event: int,
    squad: dict[tuple[int, int], pulp.LpVariable],
    constraints: SquadConstraints,
    budget: int,
) -> None:
    """The 15-man squad rules, applied independently in every gameweek."""
    for position, limit in constraints.position_limits.items():
        problem += (
            pulp.lpSum(
                squad[pid, event] for pid in ids if by_id[pid].candidate.element_type == position
            )
            == limit
        )

    problem += (
        pulp.lpSum(squad[pid, event] * by_id[pid].candidate.now_cost for pid in ids) <= budget
    )

    clubs = {by_id[pid].candidate.team_id for pid in ids}
    for team_id in clubs:
        problem += (
            pulp.lpSum(squad[pid, event] for pid in ids if by_id[pid].candidate.team_id == team_id)
            <= constraints.max_per_club
        )


def _add_starting_xi_constraints(
    problem: pulp.LpProblem,
    ids: list[int],
    by_id: dict[int, HorizonCandidate],
    event: int,
    squad: dict[tuple[int, int], pulp.LpVariable],
    start: dict[tuple[int, int], pulp.LpVariable],
    captain: dict[tuple[int, int], pulp.LpVariable],
) -> None:
    """A legal XI each week, and exactly one captain drawn from it.

    Choosing the XI inside the optimization rather than after it is a real
    improvement on the single-gameweek path, which maximizes the 15-man total
    and then picks an XI from whatever it bought. Maximizing over all fifteen
    values a fourth goalkeeper the same as a first-choice striker; maximizing
    over the eleven that actually score puts the money where the points are.
    """
    for player_id in ids:
        problem += start[player_id, event] <= squad[player_id, event]
        problem += captain[player_id, event] <= start[player_id, event]

    problem += pulp.lpSum(start[pid, event] for pid in ids) == STARTING_XI_SIZE
    problem += pulp.lpSum(captain[pid, event] for pid in ids) == 1

    for position, (low, high) in FORMATION_LIMITS.items():
        in_position = [pid for pid in ids if by_id[pid].candidate.element_type == position]
        problem += pulp.lpSum(start[pid, event] for pid in in_position) >= low
        problem += pulp.lpSum(start[pid, event] for pid in in_position) <= high


def _add_chips(
    problem: pulp.LpProblem,
    objective: list[pulp.LpAffineExpression],
    chips: frozenset[str],
    ids: list[int],
    events: list[int],
    by_id: dict[int, HorizonCandidate],
    squad: dict[tuple[int, int], pulp.LpVariable],
    start: dict[tuple[int, int], pulp.LpVariable],
    captain: dict[tuple[int, int], pulp.LpVariable],
    decay: float,
    bench_weight: float,
) -> dict[str, dict[int, pulp.LpVariable]]:
    """Let the solver decide which gameweek each chip is played in.

    Every chip is a binary per gameweek, constrained to be played at most once
    *per half-season* — FPL issues two sets, one for gameweeks 1-19 and one for
    20-38, so a horizon spanning the boundary legitimately gets two of each.
    The bench boost and triple captain both multiply a chip decision by a
    selection decision, which is not linear, so each gets an auxiliary variable
    pinned below both factors — the standard linearisation of a product of
    binaries, and it needs no upper-bound constraint here because the objective
    is pushing the auxiliary up.
    """
    halves = [
        [event for event in events if event < CHIP_SECOND_HALF_FIRST_EVENT],
        [event for event in events if event >= CHIP_SECOND_HALF_FIRST_EVENT],
    ]
    plays: dict[str, dict[int, pulp.LpVariable]] = {}
    for chip in AVAILABLE_CHIPS:
        plays[chip] = {
            event: pulp.LpVariable(f"chip_{chip}_{event}", cat="Binary") for event in events
        }
        if chip in chips:
            for half in halves:
                if half:
                    problem += pulp.lpSum(plays[chip][event] for event in half) <= 1
        else:
            for variable in plays[chip].values():
                problem += variable == 0

    # The whole cost of a free hit falls in the *following* gameweek, when the
    # squad reverts. The last week of the horizon has no following gameweek in
    # the model, so a chip played there would look free — an unlimited transfer
    # budget at no price, which the solver would take every single time. It is
    # ruled out rather than mispriced: extend the horizon by a week to find out
    # whether the final week is really where it belongs.
    if events:
        problem += plays[FREE_HIT][events[-1]] == 0

    for index, event in enumerate(events):
        # FPL allows one chip per gameweek. Without this the solver stacks
        # them: a wildcard makes a whole squad's transfers free, and a bench
        # boost then scores the bench it just bought, so every chip piles into
        # whichever week has the best fixtures. The plan that comes out is
        # worth more points than any plan you are actually allowed to play.
        problem += pulp.lpSum(plays[chip][event] for chip in AVAILABLE_CHIPS) <= 1

        weight = decay**index

        if BENCH_BOOST in chips:
            for player_id in ids:
                benched_scoring = pulp.LpVariable(f"bb_{player_id}_{event}", cat="Binary")
                problem += benched_scoring <= squad[player_id, event] - start[player_id, event]
                problem += benched_scoring <= plays[BENCH_BOOST][event]
                # The bench already earns `bench_weight` of its points from
                # autosubs, so the chip is only worth the remainder.
                objective.append(
                    weight * by_id[player_id].points(event) * (1 - bench_weight) * benched_scoring
                )

        if TRIPLE_CAPTAIN in chips:
            for player_id in ids:
                tripled = pulp.LpVariable(f"tc_{player_id}_{event}", cat="Binary")
                problem += tripled <= captain[player_id, event]
                problem += tripled <= plays[TRIPLE_CAPTAIN][event]
                objective.append(weight * by_id[player_id].points(event) * tripled)

    return plays


def _extract_plan(
    problem: pulp.LpProblem,
    by_id: dict[int, HorizonCandidate],
    events: list[int],
    squad: dict[tuple[int, int], pulp.LpVariable],
    start: dict[tuple[int, int], pulp.LpVariable],
    captain: dict[tuple[int, int], pulp.LpVariable],
    buy: dict[tuple[int, int], pulp.LpVariable],
    sell: dict[tuple[int, int], pulp.LpVariable],
    free_balance: dict[int, pulp.LpVariable],
    hits: dict[int, pulp.LpVariable],
    chip_plays: dict[str, dict[int, pulp.LpVariable]],
    chips: frozenset[str],
    free_transfers: int,
    decay: float,
) -> MultiPeriodPlan:
    gameweeks = []
    total_points = 0.0
    total_hits = 0

    for index, event in enumerate(events):
        picked = [pid for pid in by_id if _is_set(squad[pid, event])]
        # Each gameweek gets its own view of the same players, carrying that
        # week's points, so every existing display path — the starting XI
        # picker, the API schemas, the CLI printer — works on it unchanged.
        weekly = [
            replace(by_id[pid].candidate, predicted_points=by_id[pid].points(event))
            for pid in picked
        ]
        weekly_by_id = {c.player_id: c for c in weekly}
        xi = select_starting_xi(weekly)

        hits_taken = int(round(hits[event].value() or 0))
        hit_cost = TRANSFER_HIT_COST * hits_taken
        available = (
            free_transfers
            if index == 0
            else int(round(free_balance[events[index - 1]].value() or 0))
        )
        played = [name for name in sorted(chips) if _is_set(chip_plays[name][event])]
        chip = played[0] if played else None

        # The captain scores double as standard; the chips add the third
        # multiplier and the bench on top of that, which `StartingXI` already
        # quantifies for the single-gameweek path.
        expected = xi.starting_predicted_points + xi.captain.predicted_points
        if chip == TRIPLE_CAPTAIN:
            expected += xi.triple_captain_value
        if chip == BENCH_BOOST:
            expected += xi.bench_boost_value
        expected -= hit_cost

        gameweeks.append(
            GameweekPlan(
                event=event,
                squad=OptimizedSquad(
                    players=weekly,
                    total_cost=sum(c.now_cost for c in weekly),
                    total_predicted_points=sum(c.predicted_points for c in weekly),
                ),
                starting_xi=xi,
                transfers_in=[weekly_by_id[pid] for pid in picked if _is_set(buy[pid, event])],
                transfers_out=[
                    replace(by_id[pid].candidate, predicted_points=by_id[pid].points(event))
                    for pid in by_id
                    if _is_set(sell[pid, event])
                ],
                free_transfers_available=available,
                hits_taken=hits_taken,
                hit_cost=hit_cost,
                chips_played=played,
                expected_points=expected,
            )
        )
        total_points += expected
        total_hits += hit_cost

    return MultiPeriodPlan(
        gameweeks=gameweeks,
        total_expected_points=total_points,
        total_hit_cost=total_hits,
        objective_value=pulp.value(problem.objective) or 0.0,
        solver_status=pulp.LpStatus[problem.status],
    )


def _has_legal_squad(
    squad: dict[tuple[int, int], pulp.LpVariable],
    ids: list[int],
    events: list[int],
    constraints: SquadConstraints,
) -> bool:
    """Whether the solver left behind an assignment that is actually a squad.

    Checked rather than assumed because an infeasible solve still populates the
    variables with fractional relaxation values, which `_is_set` will happily
    round into a squad of the wrong size.
    """
    return all(
        sum(1 for player_id in ids if _is_set(squad[player_id, event])) == constraints.squad_size
        for event in events
    )


def _is_set(variable: pulp.LpVariable) -> bool:
    """Whether a binary came back from the solver as 1.

    CBC returns floats, and a variable the solver considers 1 can come back as
    0.9999999998, so this compares against a midpoint rather than to 1.
    """
    value = variable.value()
    return value is not None and value > 0.5
