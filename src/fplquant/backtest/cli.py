import argparse
import datetime as dt
import json
import logging
import os
from typing import Any

from fplquant.backtest.current import (
    CURRENT_SEASON,
    RoundRecord,
    complete_rounds,
    run_current_backtest,
    start_calibration,
)
from fplquant.backtest.replay import (
    DEFAULT_FIRST_ROUND,
    FPL_XP_METHOD,
    ROLLING_WINDOW,
    run_backtest,
)
from fplquant.data.history import SEASONS_WITH_STARTS
from fplquant.models.base import session_scope


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay past gameweeks and score the engine against a point-in-time baseline."
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
    parser.add_argument(
        "--with-fpl-xp",
        action="store_true",
        help=(
            "Diagnostic only. Scores the archive's xP column, which saw the results it is "
            "nominally forecasting — the same player's xP runs 1.44 points higher in the weeks "
            "he scored. Also narrows the pool to players it has an opinion about, so the other "
            "methods' numbers shift too and are not comparable with a normal run."
        ),
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help=(
            "Replay the season in progress from the live tables instead of the archive. "
            "The minutes model has not seen this season, so unlike the archive replay its "
            "contribution is measured honestly rather than being a diagnostic — which is why "
            "it is on by default here and off by default there."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "With --current, emit the results as JSON on stdout instead of a table, for a "
            "scheduled job to store. Includes the commit the engine was at, because a stored "
            "score means 'what this version of the engine predicted' and that drifts."
        ),
    )
    parser.add_argument(
        "--no-minutes-model",
        action="store_true",
        help=(
            "With --current, force the hand-built heuristic instead of the trained model. "
            "The comparison between the two is the point of the current-season replay, so "
            "this is the other half of it, not a diagnostic."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.current:
        if args.with_minutes_model:
            # That flag exists to *enable* the model on the archive replay, where
            # it is off because the model was fitted on those seasons. Here it is
            # on already, so honouring the name would be a no-op and ignoring it
            # silently would let someone believe they had changed something.
            parser.error(
                "--with-minutes-model applies to the archive replay, where the model is off "
                "by default. With --current it is already on; pass --no-minutes-model to "
                "turn it off."
            )
        _run_current(use_minutes_model=not args.no_minutes_model, as_json=args.json)
        return
    with session_scope() as session:
        result = run_backtest(
            session,
            args.seasons,
            first_round=args.first_round,
            last_round=args.last_round,
            use_minutes_model=args.with_minutes_model,
            include_fpl_xp=args.with_fpl_xp,
        )

    if not result.rounds:
        print("Nothing to replay. Run `fplquant-import-history` first.")
        return

    summary = result.summary()
    print(f"\nReplayed {len(result.rounds)} gameweeks across {len(args.seasons)} seasons.")
    if args.with_minutes_model:
        print("*** --with-minutes-model: the model saw these seasons. Diagnostic only. ***")
    if args.with_fpl_xp:
        print(f"*** --with-fpl-xp: {FPL_XP_METHOD} saw the results. Not a baseline. ***")
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
    baseline = summary.get("rolling_mean")
    if engine and baseline:
        verdict = "beats" if engine.rank_correlation > baseline.rank_correlation else "loses to"
        print(f"\n  The engine {verdict} a rolling {ROLLING_WINDOW}-gameweek mean on ranking.")


def _current_payload(
    records: list[RoundRecord], rounds: list[int], use_minutes_model: bool
) -> dict[str, Any]:
    """The results as data, stamped with the engine version that produced them.

    Every round is recomputed on every run rather than appended to, so the file
    always describes one coherent version of the engine. A score kept from an
    older commit would silently mean something different from the ones beside
    it.
    """
    payload: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        # The hash of the `src` tree, not the commit. A results file commits
        # itself, which moves HEAD — so comparing against the commit would make
        # every run look like a new engine and the job would never sit still.
        # The tree hash changes when the engine changes and not otherwise.
        "engine_rev": os.environ.get("FPLQUANT_ENGINE_REV", ""),
        "season": CURRENT_SEASON,
        "minutes_model": use_minutes_model,
        "rounds": [],
    }
    for record in records:
        result = record.result()
        if result is None:
            continue
        payload["rounds"].append(
            {
                "round": record.round,
                "players": result.players,
                # Round 1 has no earlier gameweek, so the rolling-mean baseline
                # is zero for everyone and the comparison is not one.
                "cold_start": record.round == 1,
                "scores": {
                    name: {
                        "mae": round(score.mean_absolute_error, 4),
                        "rank_correlation": round(score.rank_correlation, 4),
                        "realised_top_11": round(score.realised_top_11, 1),
                    }
                    for name, score in sorted(result.scores.items())
                },
            }
        )

    starts = [row for record in records if record.round > 1 for row in record.starts]
    payload["start_calibration"] = {
        "n": len(starts),
        "bins": [
            {"n": count, "predicted": round(predicted, 4), "observed": round(observed, 4)}
            for count, predicted, observed in start_calibration(starts)
        ],
    }
    payload["scored_rounds"] = rounds
    return payload


def _run_current(use_minutes_model: bool, as_json: bool = False) -> None:
    """Replay the season being played and report what it says.

    Round 1 is printed apart from the rest. With no earlier gameweek the
    rolling-mean baseline is zero for every player, so the comparison is not
    one — it measures the cold start, which is a different question from the
    one the other rounds answer.
    """
    with session_scope() as session:
        rounds = complete_rounds(session)
        if not rounds:
            print("No complete rounds yet. A gameweek is scored once all ten fixtures are in.")
            return
        records = run_current_backtest(session, rounds=rounds, use_minutes_model=use_minutes_model)

    if not records:
        print("Nothing scoreable yet.")
        return

    if as_json:
        print(json.dumps(_current_payload(records, rounds, use_minutes_model), indent=2))
        return

    state = "on" if use_minutes_model else "off"
    print(f"\nReplayed rounds {rounds} of the season in progress (minutes model {state}).")
    print(f"\n  {'round':<7}{'n':>6}{'method':>14}{'MAE':>9}{'rank corr':>11}{'top-11':>9}")
    for record in records:
        result = record.result()
        if result is None:
            continue
        for name, score in sorted(result.scores.items()):
            note = "   (cold start)" if record.round == 1 else ""
            print(
                f"  {record.round:<7}{result.players:>6}{name:>14}"
                f"{score.mean_absolute_error:>9.3f}{score.rank_correlation:>11.3f}"
                f"{score.realised_top_11:>9.1f}{note}"
            )

    starts = [row for record in records if record.round > 1 for row in record.starts]
    calibration = start_calibration(starts)
    if calibration:
        print(f"\n  Start-probability calibration, rounds past the cold start (n={len(starts)}):")
        print(f"    {'n':>6}{'predicted':>11}{'observed':>10}{'gap':>8}")
        for count, predicted, observed in calibration:
            print(f"    {count:>6}{predicted:>11.3f}{observed:>10.3f}{predicted - observed:>+8.3f}")
        print("\n    p_start is multiplied into expected points rather than thresholded, so")
        print("    the gap column is the one that matters: a predicted 0.6 has to mean 60%.")


if __name__ == "__main__":
    main()
