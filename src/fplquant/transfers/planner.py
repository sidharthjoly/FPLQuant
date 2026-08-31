from dataclasses import dataclass
from typing import Literal

import pulp

from fplquant.optimizer.squad import optimize_squad
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

TRANSFER_HIT_COST = 4  # points deducted per transfer beyond the free ones (real FPL rule)
# A tiny per-transfer penalty so a zero-value swap is never suggested just
# because the solver was indifferent between it and keeping the incumbent.
_CHURN_EPSILON = 0.01

# What a benched player is worth. Not zero — the autosubs are real, and a
# bench that can cover a late withdrawal has value — but nothing like a
# starter's. Matches `fplquant.optimizer.multiperiod.DEFAULT_BENCH_WEIGHT`,
# because the two solvers answering the same question differently is how a
# planner and a transfer suggestion end up contradicting each other.
BENCH_WEIGHT = 0.12

STARTING_XI_SIZE = 11
FORMATION_LIMITS: dict[int, tuple[int, int]] = {
    GOALKEEPER: (1, 1),
    DEFENDER: (3, 5),
    MIDFIELDER: (2, 5),
    FORWARD: (1, 3),
}

ChipContext = Literal["none", "wildcard", "free_hit"]


@dataclass(frozen=True)
class TransferPair:
    out: PlayerCandidate
    in_: PlayerCandidate


@dataclass(frozen=True)
class TransferPlan:
    chip: ChipContext
    transfers: list[TransferPair]
    transfers_made: int
    free_transfers: int
    hit_cost: int  # points deducted; always 0 for a wildcard/free hit
    points_gain_before_hit: float  # resulting squad's expected points minus the current squad's
    points_gain_after_hit: float
    worth_it: bool  # is making these transfers actually recommended
    resulting_squad: OptimizedSquad
    starting_xi: StartingXI


def propose_transfers(
    current_squad: list[PlayerCandidate],
    all_candidates: list[PlayerCandidate],
    bank: int,
    free_transfers: int,
    max_per_club: int = 3,
    chip: ChipContext = "none",
) -> TransferPlan:
    """Recommend the transfers (if any) that maximize next-match expected
    points net of the -4-per-transfer hit beyond your free transfers.

    "Make no transfers" is always a feasible, zero-cost choice, so the
    solver only ever recommends a transfer when its expected point gain
    outweighs its cost — this *is* the "is it worth the hit" question,
    answered by construction rather than as an afterthought.

    The gain is counted on the starting XI, with the bench discounted to
    `BENCH_WEIGHT`. Counting all fifteen equally is what made this recommend
    paying a hit to upgrade a substitute goalkeeper.

    `chip="wildcard"` or `"free_hit"` ignores `free_transfers` and the
    per-transfer hit entirely (both chips remove the transfer limit for the
    gameweek) and just rebuilds the strongest possible squad within budget.
    Sell price is approximated as each owned player's current market price
    (`now_cost`) — real FPL applies a sell-on fee that can reduce this if
    the player's price has risen since purchase, which we don't have data
    for, so available budget may be slightly overstated in that case.
    """
    current_ids = {c.player_id for c in current_squad}
    budget = sum(c.now_cost for c in current_squad) + bank

    if chip in ("wildcard", "free_hit"):
        resulting = optimize_squad(
            all_candidates, SquadConstraints(budget=budget, max_per_club=max_per_club)
        )
        xi = select_starting_xi(resulting.players)
        resulting_ids = {c.player_id for c in resulting.players}
        pairs = _pair_transfers(
            [c for c in current_squad if c.player_id not in resulting_ids],
            [c for c in resulting.players if c.player_id not in current_ids],
        )
        gain = (
            xi.starting_predicted_points
            - select_starting_xi(current_squad).starting_predicted_points
        )
        return TransferPlan(
            chip=chip,
            transfers=pairs,
            transfers_made=len(pairs),
            free_transfers=free_transfers,
            hit_cost=0,
            points_gain_before_hit=gain,
            points_gain_after_hit=gain,
            worth_it=True,
            resulting_squad=resulting,
            starting_xi=xi,
        )

    candidates_by_id = {c.player_id: c for c in all_candidates}
    # Owned players might be excluded from `all_candidates` (e.g. flagged
    # unavailable) — make sure "keep them" is always still an option.
    for player in current_squad:
        candidates_by_id.setdefault(player.player_id, player)
    candidates = list(candidates_by_id.values())

    problem = pulp.LpProblem("fpl_transfer_planning", pulp.LpMaximize)
    pick = {c.player_id: pulp.LpVariable(f"pick_{c.player_id}", cat="Binary") for c in candidates}
    # The eleven that actually score, chosen inside the optimization rather
    # than after it. Maximizing the 15-man total instead treats a fourth
    # goalkeeper exactly like a first-choice striker, and the consequence is
    # not theoretical: asked to plan for a real squad, this solver spent a
    # -4 hit swapping the *backup* keeper for a better backup keeper, a player
    # guaranteed never to take the pitch, because his projection counted in
    # full. `fplquant.optimizer.multiperiod` has always done this correctly;
    # this is the same construction.
    start = {c.player_id: pulp.LpVariable(f"start_{c.player_id}", cat="Binary") for c in candidates}
    hit_transfers = pulp.LpVariable("hit_transfers", lowBound=0, cat="Integer")

    transfers_made_expr = pulp.lpSum(
        pick[c.player_id] for c in candidates if c.player_id not in current_ids
    )

    problem += (
        pulp.lpSum(
            c.predicted_points
            * (start[c.player_id] + BENCH_WEIGHT * (pick[c.player_id] - start[c.player_id]))
            for c in candidates
        )
        - TRANSFER_HIT_COST * hit_transfers
        - _CHURN_EPSILON * transfers_made_expr
    )
    problem += hit_transfers >= transfers_made_expr - free_transfers
    problem += pulp.lpSum(pick[c.player_id] * c.now_cost for c in candidates) <= budget

    # A legal starting XI drawn from the squad.
    for c in candidates:
        problem += start[c.player_id] <= pick[c.player_id]
    problem += pulp.lpSum(start[c.player_id] for c in candidates) == STARTING_XI_SIZE
    for position, (low, high) in FORMATION_LIMITS.items():
        in_position = [c for c in candidates if c.element_type == position]
        problem += pulp.lpSum(start[c.player_id] for c in in_position) >= low
        problem += pulp.lpSum(start[c.player_id] for c in in_position) <= high

    by_position: dict[int, list[PlayerCandidate]] = {}
    for c in candidates:
        by_position.setdefault(c.element_type, []).append(c)
    for position, limit in SquadConstraints().position_limits.items():
        available = by_position.get(position, [])
        problem += pulp.lpSum(pick[c.player_id] for c in available) == limit

    by_club: dict[int, list[PlayerCandidate]] = {}
    for c in candidates:
        by_club.setdefault(c.team_id, []).append(c)
    for club_players in by_club.values():
        problem += pulp.lpSum(pick[c.player_id] for c in club_players) <= max_per_club

    problem.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[problem.status] != "Optimal":
        raise InfeasibleSquadError(
            f"No feasible transfer plan found (solver status: {pulp.LpStatus[problem.status]})"
        )

    resulting_players = [c for c in candidates if pick[c.player_id].value() == 1]
    resulting_ids = {c.player_id for c in resulting_players}
    pairs = _pair_transfers(
        [c for c in current_squad if c.player_id not in resulting_ids],
        [c for c in resulting_players if c.player_id not in current_ids],
    )

    transfers_made = len(pairs)
    hit_cost = TRANSFER_HIT_COST * max(0, transfers_made - free_transfers)
    resulting_points = sum(c.predicted_points for c in resulting_players)

    # Measured on the starting XI, because that is what the manager scores.
    # Comparing 15-man totals overstates the gain by whatever the bench
    # improved by — on a real squad that was 17.6 points of a reported 29.
    resulting_xi = select_starting_xi(resulting_players)
    current_xi = select_starting_xi(current_squad)
    gain_before = resulting_xi.starting_predicted_points - current_xi.starting_predicted_points
    gain_after = gain_before - hit_cost

    resulting_squad = OptimizedSquad(
        players=resulting_players,
        total_cost=sum(c.now_cost for c in resulting_players),
        total_predicted_points=resulting_points,
    )

    return TransferPlan(
        chip=chip,
        transfers=pairs,
        transfers_made=transfers_made,
        free_transfers=free_transfers,
        hit_cost=hit_cost,
        points_gain_before_hit=gain_before,
        points_gain_after_hit=gain_after,
        # Whether the move actually pays, not merely whether one was found.
        # `transfers_made > 0` asserted nothing: the solver had already decided
        # to transfer by the time it was read, so the flag was true whenever
        # anyone looked at it and the docstring's promise that this answers
        # "is it worth the hit" went unkept.
        worth_it=transfers_made > 0 and gain_after > 0,
        resulting_squad=resulting_squad,
        starting_xi=resulting_xi,
    )


def _pair_transfers(
    transfers_out: list[PlayerCandidate], transfers_in: list[PlayerCandidate]
) -> list[TransferPair]:
    """Pair outgoing/incoming players position-by-position for display.

    Both lists always have identical position-count breakdowns (the final
    squad's position composition is fixed and matches the current squad's),
    so grouping by element_type and zipping within each group is exact.
    """
    out_by_position: dict[int, list[PlayerCandidate]] = {}
    for c in transfers_out:
        out_by_position.setdefault(c.element_type, []).append(c)
    in_by_position: dict[int, list[PlayerCandidate]] = {}
    for c in transfers_in:
        in_by_position.setdefault(c.element_type, []).append(c)

    pairs = []
    for position, outs in out_by_position.items():
        ins = in_by_position.get(position, [])
        for out_player, in_player in zip(outs, ins, strict=True):
            pairs.append(TransferPair(out=out_player, in_=in_player))
    return pairs
