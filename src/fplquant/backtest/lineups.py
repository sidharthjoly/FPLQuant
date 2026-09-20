"""Scoring the thing the project is actually for: the lineup it would have picked.

`replay.py` scores `realised_top_11` — the eleven highest-projected players,
with no budget, no positions and no club limit. That is an ordering metric
wearing a squad's clothes, and it was standing in for a measurement nobody had
made. Here the real machinery runs: point-in-time state, the actual integer
program under the actual constraints, the actual starting XI and captain,
scored against what the players went on to do.

Two projection paths are scored side by side, because they are two different
answers to the same question and the gap between them is the finding:

- **engine** — `engine.horizon`, the structural model: fitted goal rates,
  usage shares, the scoring table, and the minutes model on top. This is what
  the multi-gameweek planner consumes.
- **form** — `form.fixtures`, an EWMA of recent points adjusted for opponent
  and venue. This is what `/optimize` and `/transfers` fed their solver until
  this backtest measured the gap; both now default to the engine and keep it
  as `projection="form"` for comparison.

Three numbers are worth reading together, and one of them is a trap:

- `total` is the XI plus the captain again, which is what a manager would have
  scored. Compared against FPL's published average it flatters the engine
  badly, because this rebuilds all fifteen players from a fresh budget every
  week — no transfer limits, no hits, no price rises. That is an
  infinite-wildcard manager, not a competitor.
- `best_possible` is the most those same fifteen could have returned with a
  perfect XI and a perfect captain. `total / best_possible` is the honest
  internal number: it measures the model against its own opportunity set
  rather than against a differently-constrained opponent.
- `captain_regret` is separated out deliberately. Captaincy is roughly a fifth
  of a score and a different question from squad selection, and a run that
  picks good squads while bleeding points on the armband should not be able to
  hide inside one total.

One limitation to keep in mind when reading any of it: `hydrate_current` does
not reconstruct availability, so every player looks fit. The backtested
optimizer will happily start someone who was ruled out on the Friday, which
costs it points the live product would not lose.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fplquant.backtest.current import hydrate_current
from fplquant.engine.horizon import project_horizon
from fplquant.models.orm import Player, PlayerGameweekStat
from fplquant.optimizer.candidates import build_candidates_from_db
from fplquant.optimizer.squad import optimize_squad
from fplquant.optimizer.starting_xi import select_starting_xi
from fplquant.optimizer.types import (
    OptimizedSquad,
    PlayerCandidate,
    SquadConstraints,
    StartingXI,
)

logger = logging.getLogger(__name__)

ENGINE = "engine"
FORM = "form"
PATHS = (ENGINE, FORM)

# Bands of the projection's own ordering. Unequal on purpose: the top of the
# ranking is where captaincy and chip decisions are made, so it is worth
# resolving finely, while the tail only needs to show whether the model is
# drifting as a whole.
CALIBRATION_BANDS: tuple[tuple[int, int], ...] = ((1, 5), (6, 10), (11, 20), (21, 50), (51, 200))


@dataclass(frozen=True)
class LineupScore:
    """One projection path's squad for one gameweek, and what it returned."""

    path: str
    round: int
    starting_points: float  # the XI, captain counted once
    captain_points: float  # the captain's own score, i.e. what the armband added
    captain: str
    captain_regret: float  # the best starter's score minus the captain's
    bench_points: float
    best_possible: float  # perfect XI and perfect captain from the same fifteen
    blanks: int  # starters who returned nothing
    squad_cost: int  # tenths of a million
    formation: str
    bench_boost_forecast: float  # what the engine said the bench was worth
    triple_captain_forecast: float  # ...and the extra captain

    @property
    def total(self) -> float:
        return self.starting_points + self.captain_points

    @property
    def capture(self) -> float:
        """Share of what these fifteen could have returned that was realised."""
        return self.total / self.best_possible if self.best_possible else 0.0


@dataclass(frozen=True)
class CalibrationBand:
    """Predicted against actual for one slice of the projection's ordering."""

    low: int
    high: int
    n: int
    predicted: float
    actual: float

    @property
    def gap(self) -> float:
        return self.actual - self.predicted


def actual_points(session: Session, round_number: int) -> dict[int, float]:
    """Points by FPL element id, summed so a double gameweek counts twice."""
    totals: dict[int, float] = {}
    rows = (
        session.query(PlayerGameweekStat.total_points, Player.fpl_id)
        .join(Player, Player.id == PlayerGameweekStat.player_id)
        .filter(PlayerGameweekStat.round == round_number)
        .all()
    )
    for points, fpl_id in rows:
        totals[fpl_id] = totals.get(fpl_id, 0.0) + points
    return totals


def best_possible_from(squad: list[PlayerCandidate], points: dict[int, float]) -> float:
    """The most this squad could have returned, knowing the results.

    The same XI selector, handed actual points instead of projected ones —
    which makes the ceiling a like-for-like comparison rather than a different
    idea of what a legal team is.
    """
    with_hindsight = [
        PlayerCandidate(
            player_id=player.player_id,
            web_name=player.web_name,
            team_id=player.team_id,
            team_short_name=player.team_short_name,
            element_type=player.element_type,
            now_cost=player.now_cost,
            predicted_points=points.get(player.player_id, 0.0),
        )
        for player in squad
    ]
    xi = select_starting_xi(with_hindsight)
    scored = [p.predicted_points for p in xi.starters]
    return sum(scored) + max(scored, default=0.0)


def score_lineup(
    path: str,
    round_number: int,
    squad: OptimizedSquad,
    xi: StartingXI,
    points: dict[int, float],
) -> LineupScore:
    """What this squad and XI actually returned. Pure — no database, no solver."""
    starters = [points.get(p.player_id, 0.0) for p in xi.starters]
    captain_points = points.get(xi.captain.player_id, 0.0)
    return LineupScore(
        path=path,
        round=round_number,
        starting_points=sum(starters),
        captain_points=captain_points,
        captain=xi.captain.web_name,
        captain_regret=max(starters, default=0.0) - captain_points,
        bench_points=sum(points.get(p.player_id, 0.0) for p in xi.bench),
        best_possible=best_possible_from(squad.players, points),
        blanks=sum(1 for score in starters if score == 0),
        squad_cost=sum(p.now_cost for p in squad.players),
        formation=xi.formation,
        bench_boost_forecast=xi.bench_boost_value,
        triple_captain_forecast=xi.triple_captain_value,
    )


def calibration(rounds: list[list[tuple[float, float]]]) -> list[CalibrationBand]:
    """Predicted against actual by band of the projection's own ranking.

    Ranked *within each gameweek* and only then pooled, so band 1-5 means "the
    five players the engine was most confident about that week" repeated over
    every week. Pooling first and ranking after would instead select the five
    highest projections of the season, which is a handful of players in a
    handful of fixtures and answers nothing.

    What this watches for is a top end that is too flat. If the players the
    model likes most consistently out-score their projection, then every
    decision that turns on the top of the ranking — the captain, the triple
    captain, which premium to buy — is being made on compressed numbers.
    """
    totals: dict[tuple[int, int], list[float]] = {
        band: [0.0, 0.0, 0.0] for band in CALIBRATION_BANDS
    }
    for pairs in rounds:
        ranked = sorted(pairs, key=lambda pair: -pair[0])
        for band in CALIBRATION_BANDS:
            low, high = band
            slice_ = ranked[low - 1 : high]
            if not slice_:
                continue
            totals[band][0] += sum(predicted for predicted, _ in slice_)
            totals[band][1] += sum(actual for _, actual in slice_)
            totals[band][2] += len(slice_)

    return [
        CalibrationBand(
            low=low,
            high=high,
            n=int(count),
            predicted=predicted / count,
            actual=actual / count,
        )
        for (low, high), (predicted, actual, count) in totals.items()
        if count
    ]


def _engine_candidates(
    session: Session, points: dict[int, float]
) -> tuple[list[PlayerCandidate], list[tuple[float, float]]]:
    """Candidates from the structural engine, plus (predicted, actual) pairs.

    The horizon projection is asked for a single event and read for that event
    alone. Its `discounted_points` is a multi-week aggregate, and ranking a
    one-week squad by it would treat a player with a blank next weekend as if
    he were playing.

    Untrimmed, unlike `build_horizon_candidates_from_db`: that trims the pool
    because the multi-period program has binaries per player *per gameweek*
    and cannot take six hundred of them. A single gameweek can, and the
    single-gameweek solver already does exactly that on the form path — so
    trimming here would handicap one arm of the comparison and nothing else.
    """
    fpl_ids = {player.id: player.fpl_id for player in session.query(Player).all()}
    candidates: list[PlayerCandidate] = []
    pairs: list[tuple[float, float]] = []
    for projection in project_horizon(session, horizon=1):
        event = next(iter(projection.points_by_event), None)
        if event is None:
            continue
        predicted = projection.points_by_event[event]
        candidates.append(
            PlayerCandidate(
                player_id=projection.player_id,
                web_name=projection.web_name,
                team_id=projection.team_id,
                team_short_name=projection.team_short_name,
                element_type=projection.element_type,
                now_cost=projection.now_cost,
                predicted_points=predicted,
            )
        )
        pairs.append((predicted, points.get(fpl_ids.get(projection.player_id, -1), 0.0)))
    return candidates, pairs


def run_lineup_backtest(
    source: Session,
    rounds: list[int],
    budget: int = 1000,
    max_per_club: int = 3,
    paths: tuple[str, ...] = PATHS,
) -> tuple[list[LineupScore], list[CalibrationBand]]:
    """Score every round on every path, and pool the engine's calibration.

    The season is hydrated once per round and both paths run against that one
    state, which keeps them honestly comparable and halves the work — by the
    end of a season this is seventy-odd integer programs and the hydration
    costs more than the solves.
    """
    scores: list[LineupScore] = []
    pooled: list[list[tuple[float, float]]] = []
    constraints = SquadConstraints(budget=budget, max_per_club=max_per_club)

    for round_number in rounds:
        session, _ = hydrate_current(source, round_number)
        try:
            by_fpl_id = actual_points(source, round_number)
            fpl_ids = {player.id: player.fpl_id for player in session.query(Player).all()}
            points = {
                player_id: by_fpl_id.get(fpl_id, 0.0) for player_id, fpl_id in fpl_ids.items()
            }
            engine_candidates, pairs = _engine_candidates(session, by_fpl_id)
            pooled.append(pairs)

            for path in paths:
                candidates = (
                    engine_candidates if path == ENGINE else build_candidates_from_db(session)
                )
                if not candidates:
                    logger.warning("No %s candidates for round %d; skipping", path, round_number)
                    continue
                squad = optimize_squad(candidates, constraints)
                xi = select_starting_xi(squad.players)
                scores.append(score_lineup(path, round_number, squad, xi, points))
        finally:
            session.close()

    return scores, calibration(pooled)
