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
from fplquant.transfers.search import (
    DEFAULT_PAYOFF_WEEKS,
    ChipSearch,
    search_chip_week,
)
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
    parser.add_argument(
        "--chip-week",
        choices=sorted(AVAILABLE_CHIPS),
        default=None,
        help=(
            "Instead of planning, ask which gameweek in the horizon this chip is worth most "
            "in. Solves the horizon once per week with the chip pinned there, against a "
            "baseline that never plays it."
        ),
    )
    parser.add_argument(
        "--payoff-weeks",
        type=int,
        default=DEFAULT_PAYOFF_WEEKS,
        help="With --chip-week: gameweeks of payoff each candidate week is judged over",
    )
    parser.add_argument(
        "--search-seconds",
        type=float,
        default=180.0,
        help=(
            "With --chip-week: wall-clock budget for the whole search. Generous by "
            "default because somebody at a terminal is waiting on purpose, where the "
            "API's budget assumes a request that has to come back."
        ),
    )
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

        if args.chip_week is not None:
            _print_chip_week(
                search_chip_week(
                    candidates,
                    events,
                    budget=budget,
                    current_squad_ids=owned or set(),
                    chip=args.chip_week,
                    free_transfers=args.free_transfers,
                    constraints=SquadConstraints(budget=budget, max_per_club=args.max_per_club),
                    decay=args.decay,
                    solver_time_limit=args.time_limit,
                    payoff_weeks=args.payoff_weeks,
                    search_seconds=args.search_seconds,
                )
            )
            return

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


def _print_chip_week(search: ChipSearch) -> None:
    """Which week to play the chip in, judged over equal windows.

    Two columns, because for a wildcard they disagree and only one of them
    means anything. `window` compares the same number of gameweeks after each
    candidate week. `total` is the whole-horizon difference, which for a
    permanent squad change declines with the play week no matter what the
    fixtures do — the earliest week improves every week in the window and the
    latest improves one.
    """
    comparable = search.comparable
    if not comparable:
        print(
            f"No week in this horizon has {search.payoff_weeks} gameweeks of payoff after it. "
            f"Extend --horizon, or shorten --payoff-weeks."
        )
        return

    print(
        f"{search.chip} · {search.searched} solves · judged over "
        f"{search.payoff_weeks}-gameweek windows"
    )
    print(f"\n  {'week':<14}{'window':>10}{'total':>10}")
    for week in comparable:
        label = "hold the chip" if week.is_baseline else f"GW{week.event}"
        print(
            f"  {label:<14}{week.window_gain or 0.0:>+10.2f}"
            f"{week.gain_vs_holding_the_chip:>+10.2f}"
        )

    best = comparable[0]
    if best.is_baseline:
        print("\n  No week here beats keeping it. That is a real answer for a chip you")
        print("  hold all season — the best week may simply be outside this horizon.")
    else:
        print(f"\n  Best of these: GW{best.event}, worth {best.window_gain:+.1f} points over")
        print(f"  the {search.payoff_weeks} gameweeks from it.")
    if search.horizon_biased:
        print("\n  Read the window column, not the total. A wildcard is a permanent")
        print("  upgrade, so its whole-horizon total falls with the week it is played in")
        print("  whatever the fixtures do, and ranking on it would always say 'now'.")
    if search.truncated:
        print("\n  Search was cut short: later weeks were not evaluated.")
    print(
        "\n  This ranks the gameweeks it can see. A chip you hold all season is a "
        "question\n  about the whole season, so re-run it as the horizon moves."
    )
