import argparse

from fplquant.engine.horizon import (
    DEFAULT_DECAY,
    DEFAULT_HORIZON,
    HorizonProjection,
    project_horizon,
)
from fplquant.engine.rates import TeamRating, compute_team_ratings
from fplquant.engine.simulate import DEFAULT_SIMULATIONS, simulate_event, summarize_player
from fplquant.models.base import session_scope
from fplquant.optimizer.types import POSITION_NAMES


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Project expected points over a multi-gameweek horizon."
    )
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="Gameweeks ahead")
    parser.add_argument("--decay", type=float, default=DEFAULT_DECAY, help="Per-gameweek discount")
    parser.add_argument("--top", type=int, default=20, help="Players to show")
    parser.add_argument("--position", choices=sorted(POSITION_NAMES.values()), default=None)
    parser.add_argument("--max-cost", type=float, default=None, help="Price ceiling in millions")
    parser.add_argument("--ratings", action="store_true", help="Show fitted team goal rates")
    parser.add_argument(
        "--explain", metavar="NAME", default=None, help="Break one player's projection down"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Monte Carlo the first gameweek for floors, ceilings and haul odds",
    )
    parser.add_argument("--simulations", type=int, default=DEFAULT_SIMULATIONS)
    parser.add_argument(
        "--simulate-event",
        type=int,
        default=None,
        help="Gameweek to simulate (default: the first one in the horizon with a full round)",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Seed the simulation for repeatability"
    )
    args = parser.parse_args()

    with session_scope() as session:
        if args.ratings:
            _print_ratings(compute_team_ratings(session))
            print()

        projections = project_horizon(session, horizon=args.horizon, decay=args.decay)
        simulated_event = None
        samples = {}
        if args.simulate and projections:
            simulated_event = args.simulate_event or _busiest_event(projections)
            if simulated_event is not None:
                samples = simulate_event(
                    projections,
                    simulated_event,
                    simulations=args.simulations,
                    seed=args.seed,
                )

    if not projections:
        print("No projections — ingest fixtures and players first.")
        return

    if args.explain:
        _explain(projections, args.explain)
        return

    shown = projections
    if args.position:
        wanted = {v: k for k, v in POSITION_NAMES.items()}[args.position]
        shown = [p for p in shown if p.element_type == wanted]
    if args.max_cost is not None:
        ceiling = round(args.max_cost * 10)
        shown = [p for p in shown if p.now_cost <= ceiling]

    events = [e.event for e in projections[0].events]
    header = f"{'player':<18}{'club':<5}{'£':>6}" + "".join(f"{'GW' + str(e):>7}" for e in events)
    header += f"{'total':>8}{'disc':>7}"
    if samples:
        header += f"{'floor':>7}{'ceil':>7}{'haul':>7}"
        print(f"simulated gameweek: GW{simulated_event} ({args.simulations} runs)")
    print(header)

    for projection in shown[: args.top]:
        row = (
            f"{projection.web_name:<18}{projection.team_short_name:<5}"
            f"{projection.now_cost / 10:>6.1f}"
        )
        for event in projection.events:
            label = "—" if event.is_blank else f"{event.points:.2f}"
            if event.is_double:
                label += "*"
            row += f"{label:>7}"
        row += f"{projection.total_points:>8.2f}{projection.discounted_points:>7.2f}"
        if samples:
            outcome = summarize_player(
                projection.player_id, projection.web_name, samples[projection.player_id]
            )
            row += f"{outcome.floor:>7.1f}{outcome.ceiling:>7.1f}{outcome.haul_probability:>6.0%} "
        print(row)

    print("\n— = blank gameweek, * = double gameweek")


def _busiest_event(projections: list[HorizonProjection]) -> int | None:
    """The first gameweek in the horizon where most of the league actually plays.

    Simulating the literal first event is the obvious default and a poor one: a
    round that is already half played has most clubs sitting out, so a table of
    floors and ceilings would be mostly zeros for reasons that have nothing to
    do with the players. This picks the first round with a full-ish fixture
    list instead, which is the one a manager is deciding about.
    """
    counts: dict[int, int] = {}
    for projection in projections:
        for event in projection.events:
            counts.setdefault(event.event, 0)
            if event.fixtures:
                counts[event.event] += 1
    if not counts:
        return None
    busiest = max(counts.values())
    if busiest == 0:
        return None
    return next(event for event in sorted(counts) if counts[event] >= busiest / 2)


def _print_ratings(ratings: dict[int, TeamRating]) -> None:
    rows = sorted(ratings.values(), key=lambda r: -r.attack_home / r.leak_home)
    print(f"{'club':<6}{'played':>7}{'weight':>8}{'attack':>8}{'leak':>7}   (home / away)")
    for rating in rows:
        print(
            f"{rating.short_name:<6}{rating.matches_played:>7}{rating.credibility:>8.2f}"
            f"{rating.attack_home:>8.2f}{rating.leak_home:>7.2f}"
            f"   {rating.attack_away:.2f} / {rating.leak_away:.2f}"
        )
    print("attack/leak are multipliers on the league average; leak > 1 means they concede more.")


def _explain(projections: list[HorizonProjection], name: str) -> None:
    matches = [p for p in projections if name.lower() in p.web_name.lower()]
    if not matches:
        print(f"No player matching {name!r}")
        return
    projection = matches[0]
    usage = projection.usage
    print(f"{projection.web_name} ({projection.team_short_name}, £{projection.now_cost / 10:.1f}m)")
    print(
        f"  starts {usage.p_start:.0%} of the time, {usage.expected_minutes:.0f} expected minutes; "
        f"rate estimate is {usage.rate_credibility:.0%} their own record, the rest price-implied"
    )
    print(
        f"  takes {usage.goal_share:.1%} of their club's goals and {usage.assist_share:.1%} "
        f"of the assists, at {usage.goals_per_90:.2f} goals and {usage.assists_per_90:.2f} "
        f"assists per 90"
    )
    for event in projection.events:
        if event.is_blank:
            print(f"  GW{event.event}: blank")
            continue
        for fixture in event.fixtures:
            venue = "H" if fixture.is_home else "A"
            b = fixture.breakdown
            print(
                f"  GW{event.event} vs {fixture.opponent_short_name} ({venue}): "
                f"{b.total:.2f} pts  [xG for {fixture.lambda_for:.2f}, against "
                f"{fixture.lambda_against:.2f}, clean sheet {b.clean_sheet_probability:.0%}]"
            )
            print(
                f"      appearance {b.appearance:+.2f}  goals {b.goals:+.2f}  "
                f"assists {b.assists:+.2f}  clean sheet {b.clean_sheet:+.2f}  "
                f"conceded {b.goals_conceded:+.2f}  saves {b.saves:+.2f}  "
                f"bonus {b.bonus:+.2f}  cards {b.cards:+.2f}"
            )
    print(f"  total {projection.total_points:.2f}, discounted {projection.discounted_points:.2f}")


if __name__ == "__main__":
    main()
