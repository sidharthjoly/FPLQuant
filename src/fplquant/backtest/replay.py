"""Score the points engine against what actually happened.

For each past gameweek, the world is rebuilt as it stood before that deadline
(`hydrate`), the real engine is asked for its projection, and the answer is
compared against the points players went on to score — and against the baseline
any model has to beat to be worth running: **a rolling mean** of the player's
last few gameweeks, which is roughly what the original `form`-based estimate
amounted to. It is built only from rounds strictly before the one being
predicted, so it is genuinely point-in-time.

The archive also carries an `xP` column, and it is **not** a fair baseline: it
saw the results it is nominally forecasting. Three measurements say so, none of
which a pre-deadline projection could survive.

* Taking the eleven players it ranks highest, every gameweek, realises 71.8
  points per gameweek across 131 gameweeks. A free, published projection that
  good would win the game outright for anyone who copied it.
* Holding the player and season fixed and looking only at rounds where they
  played 60+ minutes, the same player's own `xP` runs **1.44 points higher in
  the weeks they happened to score**, and 0.76 higher in the weeks they
  assisted. 88% of players show the effect.
* 40% of players who played 60+ minutes in both the preceding and following
  round, but did not feature in this one, have an `xP` of exactly 0.00.

So it is excluded from the comparison by default. `include_fpl_xp=True` scores
it anyway, under a name that says what it is, because it remains the only
external reference the archive carries and its *ordering* may still be worth
inspecting — but nothing it produces belongs in a headline.

The trained minutes model is deliberately switched off during a replay. It was
fitted on these very seasons, so leaving it on would let it recognise the
gameweeks it is being tested against — the resulting numbers would look
excellent and mean nothing. What is measured here is the structural model:
goal rates, usage shares, and the scoring table. Evaluating the learned
component end to end needs a model retrained with the replayed season held out,
which is a separate exercise from this one.

Two metrics matter for different reasons. Mean absolute error says how close
the numbers are; rank correlation says whether the ordering is right — and the
ordering is what actually picks a squad. `realised_top_11` is the blunt version
of the same question: take the eleven players a metric ranks highest and add up
what they really scored.
"""

import logging
import statistics
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import Session

from fplquant.backtest.hydrate import hydrate
from fplquant.engine.horizon import project_horizon
from fplquant.models.orm import HistoricalPlayerGameweek

logger = logging.getLogger(__name__)

# Rounds before this have too little history for any method to say much, and
# including them mostly measures how each one behaves with no data at all.
DEFAULT_FIRST_ROUND = 6
ROLLING_WINDOW = 3
TOP_N = 11
# Named so it cannot be mistaken for a fair baseline in a results table. See
# the module docstring for the three measurements behind the label.
FPL_XP_METHOD = "fpl_xp_leaky"


@dataclass(frozen=True)
class MethodScore:
    name: str
    mean_absolute_error: float
    rank_correlation: float
    realised_top_11: float


@dataclass(frozen=True)
class RoundResult:
    season: str
    round: int
    players: int
    scores: dict[str, MethodScore]


@dataclass(frozen=True)
class BacktestResult:
    rounds: list[RoundResult]

    def summary(self) -> dict[str, MethodScore]:
        """Average each method's metrics across every replayed gameweek."""
        names = {name for result in self.rounds for name in result.scores}
        summary = {}
        for name in sorted(names):
            scores = [r.scores[name] for r in self.rounds if name in r.scores]
            summary[name] = MethodScore(
                name=name,
                mean_absolute_error=statistics.fmean(s.mean_absolute_error for s in scores),
                rank_correlation=statistics.fmean(s.rank_correlation for s in scores),
                realised_top_11=statistics.fmean(s.realised_top_11 for s in scores),
            )
        return summary


def _rank_correlation(predicted: np.ndarray, actual: np.ndarray) -> float:
    """Spearman correlation, via Pearson on ranks.

    Written out rather than imported because scipy is not a dependency here and
    ranking two arrays is not worth one.
    """
    if len(predicted) < 3:
        return 0.0
    pred_ranks = np.argsort(np.argsort(predicted)).astype(np.float64)
    actual_ranks = np.argsort(np.argsort(actual)).astype(np.float64)
    if pred_ranks.std() == 0 or actual_ranks.std() == 0:
        return 0.0
    return float(np.corrcoef(pred_ranks, actual_ranks)[0, 1])


def _score(name: str, predicted: np.ndarray, actual: np.ndarray) -> MethodScore:
    order = np.argsort(-predicted)[:TOP_N]
    return MethodScore(
        name=name,
        mean_absolute_error=float(np.mean(np.abs(predicted - actual))),
        rank_correlation=_rank_correlation(predicted, actual),
        realised_top_11=float(actual[order].sum()),
    )


def replay_round(
    rows: list[HistoricalPlayerGameweek],
    season: str,
    round_number: int,
    use_minutes_model: bool = False,
    include_fpl_xp: bool = False,
) -> RoundResult | None:
    """Replay one gameweek and score every method against it.

    `use_minutes_model=True` is a diagnostic, not a measurement: the model was
    trained on these seasons, so it recognises the gameweeks it is being tested
    against and the result is optimistic by an unknown amount. Useful for
    asking *which component* the error lives in, worthless as a headline.

    `include_fpl_xp=True` adds the archive's contaminated `xP` column, and also
    narrows the pool to the players it has an opinion about — so the engine and
    rolling-mean figures it returns are not comparable with those from a normal
    run. That coupling is why it is off by default: scoring every method only
    where a leaky column happens to have a value let a broken baseline quietly
    choose the population everything else was judged on.
    """
    actual_rows = [r for r in rows if r.round == round_number]
    if not actual_rows:
        return None

    session, player_ids = hydrate(rows, up_to_round=round_number)
    try:
        projections = {
            p.player_id: p
            for p in project_horizon(session, horizon=1, use_minutes_model=use_minutes_model)
        }
    finally:
        session.close()

    # A double gameweek gives two archive rows; the outcome is their sum, which
    # is also what the projection produces for that event.
    actual_by_element: dict[int, float] = {}
    fpl_xp_by_element: dict[int, float] = {}
    for row in actual_rows:
        actual_by_element[row.element] = actual_by_element.get(row.element, 0.0) + row.total_points
        if row.expected_points is not None:
            fpl_xp_by_element[row.element] = (
                fpl_xp_by_element.get(row.element, 0.0) + row.expected_points
            )

    history: dict[int, list[int]] = {}
    for row in rows:
        if row.round < round_number:
            history.setdefault(row.element, []).append(row.total_points)

    elements, engine, fpl_xp, rolling, actual = [], [], [], [], []
    for element, points in actual_by_element.items():
        player_id = player_ids.get(element)
        projection = projections.get(player_id) if player_id else None
        if projection is None:
            continue
        if include_fpl_xp and element not in fpl_xp_by_element:
            # Only when `xP` is being scored does its coverage get to define the
            # population — and then it must, so the comparison is like for like.
            continue
        recent = history.get(element, [])[-ROLLING_WINDOW:]
        elements.append(element)
        engine.append(projection.next_event_points)
        fpl_xp.append(fpl_xp_by_element.get(element, 0.0))
        rolling.append(statistics.fmean(recent) if recent else 0.0)
        actual.append(points)

    if len(elements) < TOP_N:
        return None

    actual_array = np.array(actual, dtype=np.float64)
    scores = {
        "engine": _score("engine", np.array(engine, dtype=np.float64), actual_array),
        "rolling_mean": _score("rolling_mean", np.array(rolling, dtype=np.float64), actual_array),
    }
    if include_fpl_xp:
        scores[FPL_XP_METHOD] = _score(
            FPL_XP_METHOD, np.array(fpl_xp, dtype=np.float64), actual_array
        )
    return RoundResult(season=season, round=round_number, players=len(elements), scores=scores)


def run_backtest(
    session: Session,
    seasons: list[str] | tuple[str, ...],
    first_round: int = DEFAULT_FIRST_ROUND,
    last_round: int = 38,
    use_minutes_model: bool = False,
    include_fpl_xp: bool = False,
) -> BacktestResult:
    """Replay every gameweek in `seasons` and score the engine against baselines."""
    results: list[RoundResult] = []
    for season in seasons:
        rows = (
            session.query(HistoricalPlayerGameweek)
            .filter(HistoricalPlayerGameweek.season == season)
            .all()
        )
        if not rows:
            logger.warning("No archived rows for %s; run fplquant-import-history", season)
            continue
        if not any(row.team_h_score is not None for row in rows):
            # Without scorelines `engine.rates.played_fixtures` counts nothing
            # as played, every club sits on its prior, and the replay silently
            # measures a model with its top layer switched off. Loud, because
            # the numbers it produces otherwise look entirely plausible.
            logger.warning(
                "%s has no fixture scorelines — team ratings will not be fitted and the "
                "result is meaningless. Re-run fplquant-import-history.",
                season,
            )
        for round_number in range(first_round, last_round + 1):
            result = replay_round(
                rows,
                season,
                round_number,
                use_minutes_model=use_minutes_model,
                include_fpl_xp=include_fpl_xp,
            )
            if result is not None:
                results.append(result)
        logger.info("%s: replayed %d gameweeks", season, len(results))
    return BacktestResult(rounds=results)
