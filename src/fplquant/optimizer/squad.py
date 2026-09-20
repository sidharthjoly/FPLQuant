from collections import defaultdict

import pulp

from fplquant.optimizer.solution import is_set
from fplquant.optimizer.types import (
    InfeasibleSquadError,
    OptimizedSquad,
    PlayerCandidate,
    SquadConstraints,
)


def optimize_squad(
    candidates: list[PlayerCandidate],
    constraints: SquadConstraints | None = None,
) -> OptimizedSquad:
    """Select the 15-man squad maximizing total predicted points.

    Standard FPL constraints: exact position composition (2 GKP/5 DEF/5 MID/3 FWD
    by default), total cost within budget, and at most `max_per_club` players
    from any one real-life club. Solved as a 0/1 integer program.
    """
    constraints = constraints or SquadConstraints()
    if not candidates:
        raise InfeasibleSquadError("No candidate players supplied")

    problem = pulp.LpProblem("fpl_squad_selection", pulp.LpMaximize)
    pick = {c.player_id: pulp.LpVariable(f"pick_{c.player_id}", cat="Binary") for c in candidates}

    problem += pulp.lpSum(pick[c.player_id] * c.predicted_points for c in candidates)

    problem += pulp.lpSum(pick[c.player_id] * c.now_cost for c in candidates) <= constraints.budget

    by_position: dict[int, list[PlayerCandidate]] = defaultdict(list)
    for c in candidates:
        by_position[c.element_type].append(c)
    for position, limit in constraints.position_limits.items():
        available = by_position.get(position, [])
        if len(available) < limit:
            raise InfeasibleSquadError(
                f"Need {limit} players for position {position}, only {len(available)} available"
            )
        problem += pulp.lpSum(pick[c.player_id] for c in available) == limit

    by_club: dict[int, list[PlayerCandidate]] = defaultdict(list)
    for c in candidates:
        by_club[c.team_id].append(c)
    for club_players in by_club.values():
        problem += pulp.lpSum(pick[c.player_id] for c in club_players) <= constraints.max_per_club

    problem.solve(pulp.PULP_CBC_CMD(msg=False))

    if pulp.LpStatus[problem.status] != "Optimal":
        raise InfeasibleSquadError(
            f"No feasible squad found (solver status: {pulp.LpStatus[problem.status]})"
        )

    selected = [c for c in candidates if is_set(pick[c.player_id])]
    return OptimizedSquad(
        players=selected,
        total_cost=sum(c.now_cost for c in selected),
        total_predicted_points=sum(c.predicted_points for c in selected),
    )
