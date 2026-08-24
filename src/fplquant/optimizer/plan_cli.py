import argparse

from fplquant.data.fpl_client import FPLClient
from fplquant.engine.horizon import DEFAULT_DECAY, DEFAULT_HORIZON
from fplquant.models.base import session_scope
from fplquant.optimizer.candidates import build_horizon_candidates_from_db
from fplquant.optimizer.multiperiod import (
    AVAILABLE_CHIPS,
    GameweekPlan,
    plan_horizon,
)
from fplquant.optimizer.types import POSITION_NAMES, SquadConstraints
from fplquant.transfers.team_lookup import fetch_current_squad


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan squad, transfers, and chips over a multi-gameweek horizon."
    )
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="Gameweeks ahead")
    parser.add_argument("--decay", type=float, default=DEFAULT_DECAY, help="Per-gameweek discount")
    parser.add_argument("--budget", type=float, default=100.0, help="Budget in millions")
    parser.add_argument("--max-per-club", type=int, default=3)
    parser.add_argument(
        "--team-id",
        type=int,
        default=None,
        help="Plan from a real FPL team instead of building from scratch",
    )
    parser.add_argument("--free-transfers", type=int, default=1)
    parser.add_argument(
        "--chips",
        nargs="*",
        choices=sorted(AVAILABLE_CHIPS),
        default=[],
        help="Chips the planner may schedule (each is played at most once)",
    )
    parser.add_argument("--time-limit", type=int, default=120, help="Solver time limit in seconds")
    args = parser.parse_args()

    with session_scope() as session:
        owned: set[int] | None = None
        budget = round(args.budget * 10)
        if args.team_id is not None:
            with FPLClient() as client:
                current = fetch_current_squad(client, session, args.team_id)
            owned = {player.player_id for player in current.squad}
            budget = sum(player.now_cost for player in current.squad) + current.bank
            print(
                f"Planning from FPL team {args.team_id} ({current.team_name}), "
                f"squad as of GW{current.event_id}"
            )

        candidates, events = build_horizon_candidates_from_db(
            session, horizon=args.horizon, decay=args.decay, always_include=owned
        )
        plan = plan_horizon(
            candidates,
            events,
            budget=budget,
            current_squad_ids=owned,
            free_transfers=args.free_transfers,
            constraints=SquadConstraints(budget=budget, max_per_club=args.max_per_club),
            decay=args.decay,
            chips=frozenset(args.chips),
            solver_time_limit=args.time_limit,
        )

    print(
        f"Horizon GW{events[0]}-GW{events[-1]} · {plan.total_expected_points:.1f} expected points"
        f" · {plan.total_hit_cost} points of hits · solver {plan.solver_status}"
    )
    print()
    for gameweek in plan.gameweeks:
        _print_gameweek(gameweek)
        _warn_if_part_played(gameweek)
    print(
        "Only the first gameweek's moves are meant to be executed — re-run once the "
        "next round's fixtures and news land."
    )


def _warn_if_part_played(gameweek: GameweekPlan) -> None:
    """Say so when most of a round is already in the books.

    A gameweek that has largely been played leaves most clubs with no fixture
    left in it, so their players project to zero — correctly, and in a way that
    reads exactly like a broken model if nobody says otherwise.
    """
    blanking = sum(1 for player in gameweek.squad.players if player.predicted_points == 0.0)
    if blanking > len(gameweek.squad.players) / 2:
        print(
            f"  (GW{gameweek.event} is already part-played: {blanking} of "
            f"{len(gameweek.squad.players)} have no fixture left in it)\n"
        )


def _print_gameweek(gameweek: GameweekPlan) -> None:
    xi = gameweek.starting_xi
    header = f"GW{gameweek.event}  {gameweek.expected_points:.1f} pts  {xi.formation}"
    if gameweek.chip:
        header += f"  [{gameweek.chip.replace('_', ' ').upper()}]"
    print(header)
    print(
        f"  free transfers {gameweek.free_transfers_available}"
        + (
            f", {gameweek.hits_taken} hit(s) for -{gameweek.hit_cost}"
            if gameweek.hits_taken
            else ""
        )
    )
    for out_player, in_player in zip(gameweek.transfers_out, gameweek.transfers_in, strict=False):
        print(
            f"  OUT {out_player.web_name:<16} ({out_player.predicted_points:.2f})   "
            f"IN {in_player.web_name:<16} ({in_player.predicted_points:.2f})"
        )
    print(f"  C {xi.captain.web_name}, VC {xi.vice_captain.web_name}")
    starters = sorted(xi.starters, key=lambda p: (p.element_type, -p.predicted_points))
    print(
        "  XI: "
        + ", ".join(
            f"{p.web_name} ({POSITION_NAMES[p.element_type]} {p.predicted_points:.1f})"
            for p in starters
        )
    )
    print("  Bench: " + ", ".join(p.web_name for p in xi.bench))
    print()


if __name__ == "__main__":
    main()
