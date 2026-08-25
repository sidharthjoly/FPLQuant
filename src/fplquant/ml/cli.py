import argparse
import logging

from fplquant.ml.minutes_model import DEFAULT_HOLDOUT_SEASON, DEFAULT_MODEL_PATH, save, train
from fplquant.models.base import session_scope


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the minutes (start probability) model.")
    parser.add_argument(
        "--holdout-season",
        default=DEFAULT_HOLDOUT_SEASON,
        help="Season held out to measure against. Pass 'none' to fit on everything.",
    )
    parser.add_argument("--output", default=str(DEFAULT_MODEL_PATH))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    holdout = None if args.holdout_season.lower() == "none" else args.holdout_season

    with session_scope() as session:
        trained = train(session, holdout_season=holdout)

    from pathlib import Path

    path = save(trained, Path(args.output))
    evaluation = trained.evaluation

    if evaluation is None:
        print(f"Trained on every archived season. Saved to {path}")
        print("No holdout, so there is nothing to report — the numbers are unmeasured.")
        return

    print(
        f"Trained on {evaluation.n_train} rows, held out {evaluation.holdout_season} "
        f"({evaluation.n_test} rows)\n"
    )
    print(f"  log loss   {evaluation.log_loss:.4f}   (lower is better)")
    print(f"  ROC AUC    {evaluation.roc_auc:.4f}")
    print(f"  Brier      {evaluation.brier:.4f}")
    print(f"  accuracy   {evaluation.accuracy:.4f}")
    print("\n  calibration — a start probability is multiplied into expected points,")
    print("  so it has to mean what it says:\n")
    print(f"    {'predicted':>10}  {'actual':>7}")
    for predicted, actual in evaluation.calibration:
        bar = "#" * int(actual * 34)
        print(f"    {predicted:>10.3f}  {actual:>7.3f}  {bar}")
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
