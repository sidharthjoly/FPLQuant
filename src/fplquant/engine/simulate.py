"""Monte Carlo simulation of a gameweek, one match at a time.

Expected points are the wrong number for several of the decisions FPL actually
asks you to make. Captaincy is a bet on the upside, not on the mean — the right
captain is the one most likely to haul, and two players with identical expected
points can differ enormously in how often they return double figures. Chip
timing is a question about a tail. And "am I likely to beat my rival this week"
is a question about a whole distribution that no mean can answer.

The simulation is driven by the same top-down structure as the point estimates,
which is what makes it worth running rather than just bootstrapping past
scores. A match is simulated by drawing both sides' goals from the Poisson
rates in `fplquant.engine.rates`, and *then* allocating those goals to players
with a multinomial over the usage shares in `fplquant.engine.usage`. Because
every player in a match reads from the same two goal draws, correlation between
teammates is structural rather than estimated: a defender's clean sheet points
and his goalkeeper's arrive in exactly the same simulations, a striker and the
midfielder feeding him compete for the same goals, and a player and his direct
opponent's defence are anti-correlated for free.

That matters for a squad, which is a portfolio. Three Arsenal defenders are not
three independent bets on a clean sheet, they are one bet held three times, and
a simulation that draws them independently will understate the variance of that
squad badly. `fplquant.market.correlation` measures this after the fact from
past points; here it is a property of the model.
"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from fplquant.engine.horizon import HorizonProjection
from fplquant.engine.scoring import (
    APPEARANCE_POINTS,
    ASSIST_POINTS,
    CARD_POINTS_PER_90,
    CLEAN_SHEET_POINTS,
    CLEAN_SHEET_POSITIONS,
    CONCEDED_PENALTY_POSITIONS,
    DEFENSIVE_CONTRIBUTION_POINTS,
    DEFENSIVE_CONTRIBUTION_THRESHOLD,
    GOAL_POINTS,
    GOALS_CONCEDED_PER_PENALTY,
    SAVES_PER_GOAL_CONCEDED,
    SAVES_PER_POINT,
    SIXTY_MINUTE_POINTS,
    START_LASTS_SIXTY,
)
from fplquant.engine.usage import ASSISTED_GOAL_FRACTION
from fplquant.optimizer.types import GOALKEEPER

DEFAULT_SIMULATIONS = 5000

# The scoreline is truncated here. Ten goals for one side has happened once in
# Premier League history and contributes nothing measurable to any percentile.
_MAX_TEAM_GOALS = 10

# A substitute is on the pitch for roughly a quarter of the match, so they take
# roughly a quarter of a starter's share of the chances while they're on.
_CAMEO_SHARE = 20 / 84
_START_MINUTES_FRACTION = 84 / 90
_CAMEO_MINUTES_FRACTION = 20 / 90

# Bonus is drawn as a binomial over the three points available, matching the
# expected value from the usage model. This deliberately understates the top of
# the distribution: in reality bonus is awarded to whoever had the best match,
# so it lands in the same simulations as goals and assists rather than
# independently of them. Correcting that would mean modelling BPS itself, which
# is a much larger job for a point or two of tail.
_BONUS_MAX = 3

# A haul: the threshold at which a returning player has actually won you the
# week. Ten points is two goals for a midfielder, or a goal and a clean sheet
# with bonus for a defender.
HAUL_THRESHOLD = 10


@dataclass(frozen=True)
class PlayerOutcome:
    """The distribution of one player's points in one gameweek."""

    player_id: int
    web_name: str
    mean: float
    median: float
    stdev: float
    floor: float  # 10th percentile
    ceiling: float  # 90th percentile
    blank_probability: float  # P(2 points or fewer — played but did nothing)
    haul_probability: float  # P(at least HAUL_THRESHOLD)


@dataclass(frozen=True)
class SquadOutcome:
    """The distribution of a whole XI's points, captain included."""

    simulations: int
    mean: float
    median: float
    stdev: float
    percentiles: dict[int, float]
    captain_id: int | None
    captain_name: str | None


def simulate_event(
    projections: list[HorizonProjection],
    event: int,
    simulations: int = DEFAULT_SIMULATIONS,
    seed: int | None = None,
) -> dict[int, npt.NDArray[np.float64]]:
    """Simulate one gameweek, returning each player's sampled points.

    The returned arrays share a simulation index: column `i` of every player's
    array comes from the same simulated gameweek, which is what makes them
    summable into a squad distribution that respects the correlations.
    """
    rng = np.random.default_rng(seed)
    by_fixture = _group_by_fixture(projections, event)

    samples: dict[int, npt.NDArray[np.float64]] = {
        p.player_id: np.zeros(simulations, dtype=np.float64) for p in projections
    }

    for sides in by_fixture.values():
        # A double gameweek shows up as a player appearing in two fixtures;
        # accumulating rather than assigning is what sums the two.
        for player_id, points in _simulate_fixture(sides, simulations, rng).items():
            samples[player_id] += points
    return samples


def _group_by_fixture(
    projections: list[HorizonProjection], event: int
) -> dict[int, list[list[tuple[HorizonProjection, float, float]]]]:
    """fixture id -> two sides, each a list of (player, lambda_for, lambda_against).

    Both sides of a fixture have to be simulated together, from one pair of
    goal draws — that shared draw is the entire point.
    """
    fixtures: dict[int, dict[int, list[tuple[HorizonProjection, float, float]]]] = {}
    for projection in projections:
        for event_projection in projection.events:
            if event_projection.event != event:
                continue
            for fixture in event_projection.fixtures:
                side = fixtures.setdefault(fixture.fixture_id, {})
                side.setdefault(projection.team_id, []).append(
                    (projection, fixture.lambda_for, fixture.lambda_against)
                )
    return {fixture_id: list(sides.values()) for fixture_id, sides in fixtures.items()}


def _simulate_fixture(
    sides: list[list[tuple[HorizonProjection, float, float]]],
    simulations: int,
    rng: np.random.Generator,
) -> dict[int, npt.NDArray[np.float64]]:
    """Simulate one match and score every player in it."""
    if not sides:
        return {}

    # Every player on a side carries the same pair of rates, so one is enough.
    goals = []
    for side in sides:
        lambda_for = side[0][1]
        goals.append(np.minimum(rng.poisson(lambda_for, simulations), _MAX_TEAM_GOALS))

    results: dict[int, npt.NDArray[np.float64]] = {}
    for index, side in enumerate(sides):
        scored = goals[index]
        # With only one side's players ingested (a partially populated
        # database), fall back to the rate rather than the other side's draw.
        conceded = (
            goals[1 - index]
            if len(sides) == 2
            else np.minimum(rng.poisson(side[0][2], simulations), _MAX_TEAM_GOALS)
        )
        results.update(_score_side(side, scored, conceded, simulations, rng))
    return results


def _score_side(
    side: list[tuple[HorizonProjection, float, float]],
    scored: npt.NDArray[np.int64],
    conceded: npt.NDArray[np.int64],
    simulations: int,
    rng: np.random.Generator,
) -> dict[int, npt.NDArray[np.float64]]:
    players = [projection for projection, _, _ in side]
    count = len(players)
    lambda_conceded = side[0][2]

    p_start = np.array([p.usage.p_start for p in players])
    p_bench = np.array([p.usage.p_bench_appearance for p in players])

    started = rng.random((simulations, count)) < p_start
    cameo = (~started) & (rng.random((simulations, count)) < p_bench)
    sixty = started & (rng.random((simulations, count)) < START_LASTS_SIXTY)
    on_pitch = started | cameo

    # A player only competes for goals in the simulations where they're on the
    # pitch, and a substitute competes for a fraction of a starter's share.
    #
    # The weights are the raw per-90 *rates*, not the usage shares. The shares
    # already have expected minutes baked into them, and the presence draw
    # applies minutes again, so weighting by share would charge availability
    # twice — and then the per-simulation renormalisation would hand the excess
    # straight back to whoever is most nailed, inflating exactly the players the
    # optimizer is most likely to pick. Rates carry no minutes term, so drawing
    # presence and normalising across whoever is on the pitch reproduces the
    # analytic shares in expectation.
    presence = started.astype(np.float64) + cameo.astype(np.float64) * _CAMEO_SHARE
    goal_weights = presence * np.array([p.usage.goals_per_90 for p in players])
    assist_weights = presence * np.array([p.usage.assists_per_90 for p in players])

    goals_by_player = _allocate(scored, goal_weights, rng)
    # Only assisted goals pay anyone; the rest are solo efforts and penalties.
    assisted = rng.binomial(scored, ASSISTED_GOAL_FRACTION)
    assists_by_player = _allocate(assisted, assist_weights, rng)

    clean_sheet = (conceded == 0)[:, None] & sixty
    concede_penalty = (conceded // GOALS_CONCEDED_PER_PENALTY)[:, None] * on_pitch
    saves = rng.poisson(SAVES_PER_GOAL_CONCEDED * lambda_conceded, (simulations, count))

    results = {}
    for index, projection in enumerate(players):
        position = projection.element_type
        points = np.zeros(simulations, dtype=np.float64)
        points += APPEARANCE_POINTS * on_pitch[:, index]
        points += SIXTY_MINUTE_POINTS * sixty[:, index]
        points += GOAL_POINTS[position] * goals_by_player[:, index]
        points += ASSIST_POINTS * assists_by_player[:, index]
        if position in CLEAN_SHEET_POSITIONS:
            points += CLEAN_SHEET_POINTS[position] * clean_sheet[:, index]
        if position in CONCEDED_PENALTY_POSITIONS:
            points -= concede_penalty[:, index]
        if position == GOALKEEPER:
            points += (saves[:, index] // SAVES_PER_POINT) * sixty[:, index]

        bonus_rate = min(1.0, max(0.0, projection.usage.bonus_per_appearance / _BONUS_MAX))
        points += rng.binomial(_BONUS_MAX, bonus_rate, simulations) * on_pitch[:, index]

        minutes = (
            started[:, index] * _START_MINUTES_FRACTION + cameo[:, index] * _CAMEO_MINUTES_FRACTION
        )
        points += CARD_POINTS_PER_90[position] * minutes

        # Defensive Contribution: a threshold on a count, so it is drawn as one
        # — Poisson over the minutes that simulation actually played, matching
        # the closed form's P(actions >= threshold). Sampling it rather than
        # adding its expectation is the point of having two implementations:
        # the analytic term evaluates the tail exactly, the sampler reaches it
        # by counting, and if they disagree one of them is wrong.
        threshold = DEFENSIVE_CONTRIBUTION_THRESHOLD.get(position)
        rate = projection.usage.defensive_actions_per_90
        if threshold is not None and rate > 0:
            actions = rng.poisson(rate * np.maximum(minutes, 0.0))
            points += DEFENSIVE_CONTRIBUTION_POINTS * (actions >= threshold)

        results[projection.player_id] = points
    return results


def _allocate(
    goals: npt.NDArray[np.int64],
    weights: npt.NDArray[np.float64],
    rng: np.random.Generator,
) -> npt.NDArray[np.int64]:
    """Hand out each simulation's goals to players, weighted by usage share.

    A multinomial per simulation, but with per-simulation probabilities (they
    depend on who happened to be on the pitch), which numpy's `multinomial`
    can't vectorise over. Inverse-transform sampling against the row-wise
    cumulative weights does the same job in one pass.

    Rows whose weights are all zero — nobody on the pitch, which happens when
    every modelled player on a side is benched in that simulation — drop their
    goals rather than forcing them onto someone. Those goals belong to players
    outside the pool, which is the honest place for them.
    """
    simulations, count = weights.shape
    allocation = np.zeros((simulations, count), dtype=np.int64)
    if count == 0:
        return allocation

    totals = weights.sum(axis=1, keepdims=True)
    live = totals[:, 0] > 0
    if not live.any():
        return allocation

    cumulative = np.cumsum(
        np.where(totals > 0, weights / np.where(totals > 0, totals, 1), 0), axis=1
    )
    draws = rng.random((simulations, _MAX_TEAM_GOALS))
    # For each draw, the first player whose cumulative weight exceeds it.
    chosen = (draws[:, :, None] > cumulative[:, None, :]).sum(axis=2)

    slots = np.arange(_MAX_TEAM_GOALS)[None, :]
    counted = (slots < goals[:, None]) & live[:, None] & (chosen < count)

    rows = np.repeat(np.arange(simulations), _MAX_TEAM_GOALS).reshape(simulations, -1)
    np.add.at(allocation, (rows[counted], chosen[counted]), 1)
    return allocation


def summarize_player(
    player_id: int, web_name: str, samples: npt.NDArray[np.float64]
) -> PlayerOutcome:
    """Turn one player's simulated points into the numbers a manager reads."""
    return PlayerOutcome(
        player_id=player_id,
        web_name=web_name,
        mean=float(np.mean(samples)),
        median=float(np.median(samples)),
        stdev=float(np.std(samples)),
        floor=float(np.percentile(samples, 10)),
        ceiling=float(np.percentile(samples, 90)),
        blank_probability=float(np.mean(samples <= 2)),
        haul_probability=float(np.mean(samples >= HAUL_THRESHOLD)),
    )


def summarize_squad(
    samples: dict[int, npt.NDArray[np.float64]],
    starter_ids: list[int],
    captain_id: int | None = None,
    captain_name: str | None = None,
    captain_multiplier: int = 2,
) -> SquadOutcome:
    """Add up an XI's simulated points, keeping the simulations aligned.

    Summing the per-player arrays element-wise rather than summing their means
    is the whole reason to run the simulation: it preserves the fact that a
    squad's good weeks and bad weeks arrive together.
    """
    if not starter_ids:
        raise ValueError("No starters to summarize")
    total = sum(samples[player_id] for player_id in starter_ids if player_id in samples)
    if isinstance(total, int):
        raise ValueError("None of the starters were simulated")
    if captain_id is not None and captain_id in samples:
        total = total + (captain_multiplier - 1) * samples[captain_id]

    return SquadOutcome(
        simulations=int(total.shape[0]),
        mean=float(np.mean(total)),
        median=float(np.median(total)),
        stdev=float(np.std(total)),
        percentiles={p: float(np.percentile(total, p)) for p in (5, 25, 50, 75, 95)},
        captain_id=captain_id,
        captain_name=captain_name,
    )
