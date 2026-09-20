import argparse

from fplquant.models.base import session_scope
from fplquant.optimizer.candidates import (
    ENGINE,
    FORM,
    build_candidates_from_db,
    build_risk_adjusted_candidates_from_db,
)
from fplquant.optimizer.squad import optimize_squad
from fplquant.optimizer.starting_xi import select_starting_xi
from fplquant.optimizer.types import POSITION_NAMES, PlayerCandidate, SquadConstraints


def main() -> None:
    parser = argparse.ArgumentParser(description="Select an optimal FPL squad.")
    parser.add_argument(
        "--budget", type=float, default=100.0, help="Budget in millions, e.g. 100.0"
    )
    parser.add_argument("--max-per-club", type=int, default=3)
    parser.add_argument(
        "--risk-adjusted",
        action="store_true",
        help="Maximize risk-adjusted points (form, volatility, injury risk) instead of raw points",
    )
    parser.add_argument(
        "--risk-aversion",
        type=float,
        default=1.0,
        help="Only with --risk-adjusted: higher penalizes volatility more",
    )
    parser.add_argument(
        "--injury-weight",
        type=float,
        default=1.0,
        help="Only with --risk-adjusted: higher penalizes injury risk more",
    )
    parser.add_argument(
        "--projection",
        choices=(ENGINE, FORM),
        default=ENGINE,
        help=(
            "Which projection to maximize. The engine's structural model by default; "
            "'form' is the older EWMA, kept for comparison and measurably worse — see "
            "`fplquant-backtest --lineups`."
        ),
    )
    args = parser.parse_args()

    constraints = SquadConstraints(budget=round(args.budget * 10), max_per_club=args.max_per_club)

    with session_scope() as session:
        if args.risk_adjusted:
            candidates = build_risk_adjusted_candidates_from_db(
                session,
                risk_aversion=args.risk_aversion,
                injury_weight=args.injury_weight,
                projection=args.projection,
            )
        else:
            candidates = build_candidates_from_db(session, projection=args.projection)
        squad = optimize_squad(candidates, constraints)
        xi = select_starting_xi(squad.players)

    points_label = "risk-adjusted points" if args.risk_adjusted else "predicted points"
    print(f"Total cost: £{squad.total_cost / 10:.1f}m / £{constraints.budget / 10:.1f}m")
    print(f"Total {points_label} (15-man squad): {squad.total_predicted_points:.2f}")
    print(f"Starting XI {points_label}: {xi.starting_predicted_points:.2f}")
    print(f"Formation: {xi.formation}")
    print(f"Captain: {xi.captain.web_name} (vice: {xi.vice_captain.web_name})")
    print(f"Bench Boost would add: +{xi.bench_boost_value:.2f} pts")
    print(f"Triple Captain would add: +{xi.triple_captain_value:.2f} pts")
    print()

    def _badge(player: PlayerCandidate) -> str:
        if player.player_id == xi.captain.player_id:
            return " (C)"
        if player.player_id == xi.vice_captain.player_id:
            return " (VC)"
        return ""

    def _print_group(players: list[PlayerCandidate]) -> None:
        by_position: dict[int, list[PlayerCandidate]] = {}
        for player in players:
            by_position.setdefault(player.element_type, []).append(player)
        for position in sorted(by_position):
            print(f"{POSITION_NAMES[position]}:")
            for player in sorted(by_position[position], key=lambda p: -p.predicted_points):
                print(
                    f"  {player.web_name:<20}{_badge(player):<5} {player.team_short_name:<4} "
                    f"£{player.now_cost / 10:>4.1f}m  {player.predicted_points:>6.2f} pts"
                )

    print("STARTING XI")
    _print_group(xi.starters)
    print("\nBENCH")
    _print_group(xi.bench)


if __name__ == "__main__":
    main()
