import argparse

from sqlalchemy.orm import Session

from fplquant.lineup.formation import compute_team_shapes, describe_shape
from fplquant.lineup.starts import compute_start_probabilities
from fplquant.models.base import session_scope


def _print_shapes(session: Session) -> None:
    shapes = sorted(compute_team_shapes(session), key=lambda s: s.short_name)
    header = f"{'Club':<6}{'GWs':>5}{'Shape':>8}{'Recent':>9}"
    print(header)
    print("-" * len(header))
    for shape in shapes:
        print(
            f"{shape.short_name:<6}{shape.rounds_observed:>5}"
            f"{describe_shape(shape.slots):>8}{describe_shape(shape.recent_slots):>9}"
        )


def _print_starts(session: Session, top: int) -> None:
    probabilities = compute_start_probabilities(session)
    header = (
        f"{'#':>3}  {'Player':<20}{'Apps':>6}{'Base':>8}{'Adj':>8}"
        f"{'Rest':>7}{'Load':>7}{'Fatigue':>9}{'Shape':>8}{'Lineup x':>10}"
    )
    print(header)
    print("-" * len(header))
    for rank, p in enumerate(probabilities[:top], start=1):
        rest = f"{p.rest_days:.1f}" if p.rest_days is not None else "?"
        print(
            f"{rank:>3}  {p.web_name:<20}{p.appearances:>6}"
            f"{p.baseline_probability:>8.2f}{p.adjusted_probability:>8.2f}"
            f"{rest:>7}{p.minutes_load:>7.2f}{p.fatigue_index:>9.2f}"
            f"{p.recent_team_shape:>8}{p.lineup_multiplier:>10.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print inferred club formations and next-match start probabilities."
    )
    parser.add_argument("--top", type=int, default=20, help="Number of players to show")
    parser.add_argument(
        "--shapes", action="store_true", help="Show inferred club formations instead of players"
    )
    args = parser.parse_args()

    with session_scope() as session:
        if args.shapes:
            _print_shapes(session)
        else:
            _print_starts(session, args.top)


if __name__ == "__main__":
    main()
