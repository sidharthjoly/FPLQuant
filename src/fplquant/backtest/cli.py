import argparse
import logging

from fplquant.backtest.replay import DEFAULT_FIRST_ROUND, run_backtest
from fplquant.data.history import SEASONS_WITH_STARTS
from fplquant.models.base import session_scope


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay past gameweeks and score the engine against FPL's own projection."
    )
    parser.add_argument("--seasons", nargs="+", default=list(SEASONS_WITH_STARTS))
    parser.add_argument("--first-round", type=int, default=DEFAULT_FIRST_ROUND)
    parser.add_argument("--last-round", type=int, default=38)
    parser.add_argument(
        "--with-minutes-model",
        action="store_true",
        help=(
            "Diagnostic only. The model was trained on these seasons, so it recognises the "
            "gameweeks it is tested against and the result is optimistic by an unknown amount."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with session_scope() as session:
        result = run_backtest(
            session,
            args.seasons,
            first_round=args.first_round,
            last_round=args.last_round,
            use_minutes_model=args.with_minutes_model,
        )

    if not result.rounds:
        print("Nothing to replay. Run `fplquant-import-history` first.")
        return

    summary = result.summary()
    print(f"\nReplayed {len(result.rounds)} gameweeks across {len(args.seasons)} seasons.")
    if args.with_minutes_model:
        print("*** --with-minutes-model: the model saw these seasons. Diagnostic only. ***")
    print(f"\n  {'method':<15}{'MAE':>8}{'rank corr':>11}{'top-11 pts':>12}")
    for name, score in sorted(summary.items(), key=lambda kv: -kv[1].rank_correlation):
        print(
            f"  {name:<15}{score.mean_absolute_error:>8.3f}"
            f"{score.rank_correlation:>11.3f}{score.realised_top_11:>12.1f}"
        )
    print("\n  MAE: how close the numbers are. rank corr: whether the ordering is right,")
    print("  which is what actually picks a squad. top-11 pts: what the eleven players")
    print("  each metric ranks highest really went on to score, averaged per gameweek.")

    engine = summary.get("engine")
    baseline = summary.get("fpl_xp")
    if engine and baseline:
        verdict = "beats" if engine.rank_correlation > baseline.rank_correlation else "loses to"
        print(f"\n  The engine {verdict} FPL's own xP on ranking.")


if __name__ == "__main__":
    main()
