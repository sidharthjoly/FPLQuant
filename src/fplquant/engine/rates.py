"""Team-level goal rates: how many goals each side is expected to score.

Everything downstream in `fplquant.engine` is an allocation of the two numbers
this module produces for a fixture — the home side's expected goals and the
away side's. Clean sheets, defensive returns, save points and goal involvements
all fall out of them, which is what makes a fixture swing propagate coherently
instead of being applied as an ad-hoc multiplier at the end.

The model is the standard multiplicative one:

    lambda_home = base_home * attack[home] * leak[away]
    lambda_away = base_away * attack[away] * leak[home]

where `attack` and `leak` are per-team multipliers centred on 1.0 (leak > 1
means a side concedes more than average). Fitting those 40 numbers by maximum
likelihood is the textbook approach, and it is the wrong approach here: at the
point in a season where this is most useful there are a dozen matches on
record, and 40 free parameters against 24 observed scorelines is not a noisy
fit, it is an underdetermined one.

So the multipliers start at a prior taken from FPL's own published team
strength ratings — which encode a whole previous season and the bookmakers'
view of summer transfer business — and the match record moves them by a
credibility weight that grows with the number of matches observed. With no
matches played this reduces exactly to FPL's ratings; by midseason the
published ratings are barely visible through the fitted correction. That is the
same treatment `fplquant.form.scoring` gives player form, applied a level up.
"""

import math
import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team

# Premier League goals per side per match, split by venue. Home advantage is
# worth roughly a quarter of a goal. These anchor the whole model's scale, so
# they're deliberately long-run league constants rather than something
# re-estimated from a handful of matches — a fortnight of high-scoring games
# should move individual teams' multipliers, not the league baseline.
BASE_GOALS_HOME = 1.55
BASE_GOALS_AWAY = 1.25

# Matches of evidence before a team's own record counts for as much as the
# prior. Higher than the player-level equivalent (six appearances) because a
# team's underlying quality is being inferred from a single aggregate number
# per match, where a player's form has ninety minutes of stats behind each row.
TEAM_MATCH_CREDIBILITY = 8.0
# Halflife, in matches, for down-weighting older results. Form is real at team
# level — new signings bed in, managers are sacked — but six matches is long
# enough that a single bad afternoon doesn't rewrite a rating.
TEAM_FORM_HALFLIFE = 6.0

# How far a fitted multiplier may stray from 1.0. The best and worst attacks in
# a Premier League season differ by roughly a factor of two either side of
# average, and anything outside that is small-sample noise rather than signal.
_MIN_MULTIPLIER = 0.45
_MAX_MULTIPLIER = 2.2
# Final guard on the fixture rate itself. No Premier League side has a true
# expectation below a quarter of a goal or above four.
_MIN_LAMBDA = 0.25
_MAX_LAMBDA = 4.0

# Passes of the multiplicative fixed-point fit. Each pass re-estimates every
# team's multipliers against the others' current values; because the update is
# damped by the credibility weight it converges quickly, and 30 passes is
# comfortably past the point where the numbers stop moving.
_FIT_PASSES = 30

# Weight on expected goals versus actual goals when measuring what a team did.
# xG is the better estimator of a team's underlying rate — it aggregates every
# chance in the match rather than the two or three that went in, so it settles
# down over a handful of games where goals take most of a season. Goals still
# carry a third of the weight because finishing quality is real and xG models
# systematically miss it.
XG_WEIGHT = 0.7


@dataclass(frozen=True)
class TeamRating:
    """A team's fitted attacking and defensive multipliers, both centred on 1.0."""

    team_id: int
    short_name: str
    matches_played: int
    effective_matches: float  # time-decayed match count driving the credibility weight
    credibility: float  # 0.0 (pure prior) to 1.0 (pure fitted)
    attack_home: float
    attack_away: float
    leak_home: float  # >1 means they concede more than average at home
    leak_away: float
    prior_attack_home: float
    prior_leak_home: float


@dataclass(frozen=True)
class FixtureRates:
    """Expected goals for both sides of one fixture."""

    fixture_id: int
    event: int | None
    home_team_id: int
    away_team_id: int
    lambda_home: float
    lambda_away: float


# A team's prior strength comes from two independent readings, blended
# geometrically. FPL publishes its own team ratings, which is the obvious
# source and an unreliable one: the granular attack/defence columns sit at zero
# for much of preseason, and the coarse `strength_overall_*` rating has only
# four distinct values across twenty clubs, tying half the league together. The
# second reading is the market's: the combined price of a club's fifteen most
# expensive players, which is continuous, never missing, and reprices itself as
# managers move money around. The blend leans on the market because it
# discriminates between clubs that FPL's integer rating cannot.
MARKET_PRIOR_WEIGHT = 0.6

# How hard a quality index is allowed to push goal rates. Both are below 1
# because a team twice as expensive as another is not twice as likely to score:
# squad value and league position are compressed relative to goal difference at
# the top of the table, where the marginal £30m buys a substitute. Defence gets
# the larger exponent because the spread in goals conceded across a Premier
# League season is wider than the spread in goals scored.
ATTACK_ELASTICITY = 0.55
DEFENCE_ELASTICITY = 0.70

# Players per club counted toward the market-implied strength index. Fifteen is
# an FPL squad, which is enough to separate a deep squad from a top-heavy one
# without letting a difference in how many fringe players a club has registered
# move the number.
MARKET_SQUAD_SIZE = 15


def _normalised_index(values: dict[int, float]) -> dict[int, float] | None:
    """Ratios to the pool mean, or None if the values carry no information.

    A column where every club has the same number — FPL's attack and defence
    strengths in preseason, which are simply zero for all twenty — is not a
    weak signal to be down-weighted, it is the absence of one. Returning None
    lets the caller drop it from the blend entirely rather than multiplying
    every team's prior by 1.0 and pretending a source was consulted.
    """
    if not values:
        return None
    mean = statistics.fmean(values.values())
    if mean <= 0:
        return None
    spread = max(values.values()) - min(values.values())
    if spread <= 0:
        return None
    return {key: value / mean for key, value in values.items()}


def _market_strength_index(session: Session, teams: list[Team]) -> dict[int, float] | None:
    """Each club's squad value relative to the league's, centred on 1.0.

    The market's own estimate of how good a side is. FPL prices are set by the
    game rather than by a transfer market, but they are revised continuously
    against ownership and returns, which makes them a live signal where a
    preseason strength rating is a stale one.
    """
    costs: dict[int, list[int]] = {team.id: [] for team in teams}
    for player in session.query(Player).all():
        if player.team_id in costs:
            costs[player.team_id].append(player.now_cost)
    totals = {
        team_id: float(sum(sorted(prices, reverse=True)[:MARKET_SQUAD_SIZE]))
        for team_id, prices in costs.items()
        if prices
    }
    if len(totals) < len(teams):
        return None
    return _normalised_index(totals)


def _blend_indices(sources: list[tuple[dict[int, float], float]], team_id: int) -> float:
    """Weighted geometric mean of the available quality indices for one team.

    Geometric rather than arithmetic because these are multipliers: a club
    rated 2x by one source and 0.5x by another should come out at 1.0, not at
    1.25. Weights are renormalised over whichever sources actually exist, so
    dropping one changes the blend's composition and never its scale.
    """
    total_weight = sum(weight for _, weight in sources)
    if total_weight <= 0:
        return 1.0
    log_sum = sum(weight * math.log(index[team_id]) for index, weight in sources)
    return math.exp(log_sum / total_weight)


def _venue_priors(
    session: Session, teams: list[Team]
) -> dict[int, tuple[float, float, float, float]]:
    """Prior (attack_home, attack_away, leak_home, leak_away) per team.

    Each venue gets its own quality index, built from whichever sources are
    informative, and the index is then turned into a goal multiplier by an
    elasticity. A team's *leak* is the reciprocal of their defensive quality:
    the ratings go up as a defence gets better, and what the goal model needs is
    a number that goes up as a defence gets worse.
    """
    market = _market_strength_index(session, teams)

    def strength_index(attribute: str, fallback: str) -> dict[int, float] | None:
        index = _normalised_index({t.id: float(getattr(t, attribute)) for t in teams})
        if index is not None:
            return index
        return _normalised_index({t.id: float(getattr(t, fallback)) for t in teams})

    indices = {
        "attack_home": strength_index("strength_attack_home", "strength_overall_home"),
        "attack_away": strength_index("strength_attack_away", "strength_overall_away"),
        "defence_home": strength_index("strength_defence_home", "strength_overall_home"),
        "defence_away": strength_index("strength_defence_away", "strength_overall_away"),
    }

    def combined(key: str, team_id: int) -> float:
        sources: list[tuple[dict[int, float], float]] = []
        fpl_index = indices[key]
        if fpl_index is not None:
            sources.append((fpl_index, 1 - MARKET_PRIOR_WEIGHT))
        if market is not None:
            sources.append((market, MARKET_PRIOR_WEIGHT))
        return _blend_indices(sources, team_id)

    priors = {}
    for team in teams:
        attack_home = combined("attack_home", team.id) ** ATTACK_ELASTICITY
        attack_away = combined("attack_away", team.id) ** ATTACK_ELASTICITY
        leak_home = combined("defence_home", team.id) ** -DEFENCE_ELASTICITY
        leak_away = combined("defence_away", team.id) ** -DEFENCE_ELASTICITY
        priors[team.id] = (
            _clamp(attack_home, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
            _clamp(attack_away, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
            _clamp(leak_home, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
            _clamp(leak_away, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
        )
    return priors


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _observed_team_xg(session: Session) -> dict[tuple[int, int], float]:
    """Total expected goals per (fixture_fpl_id, team_id), summed over players.

    Attribution goes through the fixture rather than through `Player.team_id`,
    so a player who moved clubs midseason has their past matches credited to
    the club they actually played them for.
    """
    rows = (
        session.query(PlayerGameweekStat, Fixture)
        .join(Fixture, Fixture.fpl_id == PlayerGameweekStat.fixture_fpl_id)
        .filter(PlayerGameweekStat.fixture_fpl_id.isnot(None))
        .all()
    )
    totals: dict[tuple[int, int], float] = {}
    for stat, fixture in rows:
        if stat.was_home is None:
            continue
        team_id = fixture.team_h_id if stat.was_home else fixture.team_a_id
        key = (fixture.fpl_id, team_id)
        totals[key] = totals.get(key, 0.0) + stat.expected_goals
    return totals


def played_fixtures(session: Session) -> list[Fixture]:
    """Finished fixtures with a scoreline, oldest first.

    `Fixture.finished` is already the permissive flag — ingestion sets it from
    either FPL's `finished` or `finished_provisional` — so a match that ended
    an hour ago counts here even though its bonus points aren't confirmed.
    """
    fixtures = (
        session.query(Fixture)
        .filter(
            Fixture.finished.is_(True),
            Fixture.team_h_score.isnot(None),
            Fixture.team_a_score.isnot(None),
        )
        .all()
    )
    return sorted(fixtures, key=lambda f: (f.kickoff_time is None, f.kickoff_time, f.id))


def _recency_weights(fixtures: list[Fixture], halflife: float) -> list[float]:
    """One weight per fixture, decaying with how many *gameweeks* ago it was.

    Deliberately keyed on the gameweek rather than on position in the fixture
    list. Matches inside one round are equally recent, and decaying across them
    would quietly rate a club that played the Friday night game as less known
    than one that played on Sunday — an artefact of the broadcast schedule, not
    of anything the model should care about. Postponed fixtures carry no event
    number at all; they inherit the round of the last fixture before them,
    which is where they sit in the calendar even if FPL has yet to reassign them.
    """
    if not fixtures:
        return []
    if halflife <= 0:
        return [1.0] * len(fixtures)

    rounds: list[int] = []
    last_seen = 0
    for fixture in fixtures:
        if fixture.event is not None:
            last_seen = fixture.event
        rounds.append(last_seen)

    latest = max(rounds)
    decay = 0.5 ** (1.0 / halflife)
    return [decay ** (latest - round_number) for round_number in rounds]


def compute_team_ratings(
    session: Session,
    halflife: float = TEAM_FORM_HALFLIFE,
    credibility_matches: float = TEAM_MATCH_CREDIBILITY,
) -> dict[int, TeamRating]:
    """Fit every team's attack and leak multipliers from the results so far.

    The fit is a damped multiplicative fixed point. Each pass asks, for every
    team: given what the model currently believes about the opponents they
    faced, how many goals *should* they have scored, and how many did they
    actually score? The ratio of the two is the correction to their attacking
    multiplier, and the same question run the other way round corrects their
    defensive one. Because each team's correction depends on its opponents'
    current ratings, the passes are iterated until the numbers settle — a side
    that beat three of last season's relegation candidates ends up rated below
    one that took the same points off the top three.

    Raising the correction to the power of the credibility weight is what makes
    this safe on small samples: at weight 0 every ratio collapses to 1.0 and
    the priors come through untouched, at weight 1 the fit is taken at face
    value, and in between the correction is geometrically damped. It doubles as
    the step size that makes the iteration converge.
    """
    teams = session.query(Team).all()
    if not teams:
        return {}

    priors = _venue_priors(session, teams)
    fixtures = played_fixtures(session)
    xg_by_fixture_team = _observed_team_xg(session)

    weights = _recency_weights(fixtures, halflife)

    played_count: dict[int, int] = dict.fromkeys((t.id for t in teams), 0)
    effective: dict[int, float] = dict.fromkeys((t.id for t in teams), 0.0)
    for fixture, weight in zip(fixtures, weights, strict=True):
        for team_id in (fixture.team_h_id, fixture.team_a_id):
            if team_id in played_count:
                played_count[team_id] += 1
                effective[team_id] += weight

    credibility = {
        team_id: n / (n + credibility_matches) if n > 0 else 0.0 for team_id, n in effective.items()
    }

    attack = {t.id: (priors[t.id][0], priors[t.id][1]) for t in teams}
    leak = {t.id: (priors[t.id][2], priors[t.id][3]) for t in teams}

    # What each side actually produced, blending xG with the scoreline. Held
    # outside the loop since it doesn't depend on the current ratings.
    observed: dict[int, tuple[float, float]] = {}
    for fixture in fixtures:
        home_goals = float(fixture.team_h_score or 0)
        away_goals = float(fixture.team_a_score or 0)
        home_xg = xg_by_fixture_team.get((fixture.fpl_id, fixture.team_h_id))
        away_xg = xg_by_fixture_team.get((fixture.fpl_id, fixture.team_a_id))
        observed[fixture.id] = (
            _blend_goals(home_goals, home_xg),
            _blend_goals(away_goals, away_xg),
        )

    for _ in range(_FIT_PASSES):
        scored: dict[int, list[float]] = {t.id: [0.0, 0.0] for t in teams}  # [observed, expected]
        conceded: dict[int, list[float]] = {t.id: [0.0, 0.0] for t in teams}

        for fixture, weight in zip(fixtures, weights, strict=True):
            home_id, away_id = fixture.team_h_id, fixture.team_a_id
            if home_id not in scored or away_id not in scored:
                continue
            lambda_home = BASE_GOALS_HOME * attack[home_id][0] * leak[away_id][1]
            lambda_away = BASE_GOALS_AWAY * attack[away_id][1] * leak[home_id][0]
            home_obs, away_obs = observed[fixture.id]

            scored[home_id][0] += weight * home_obs
            scored[home_id][1] += weight * lambda_home
            scored[away_id][0] += weight * away_obs
            scored[away_id][1] += weight * lambda_away
            conceded[home_id][0] += weight * away_obs
            conceded[home_id][1] += weight * lambda_away
            conceded[away_id][0] += weight * home_obs
            conceded[away_id][1] += weight * lambda_home

        for team in teams:
            w = credibility[team.id]
            attack_correction = _correction(scored[team.id], w)
            leak_correction = _correction(conceded[team.id], w)
            prior = priors[team.id]
            attack[team.id] = (
                _clamp(prior[0] * attack_correction, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
                _clamp(prior[1] * attack_correction, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
            )
            leak[team.id] = (
                _clamp(prior[2] * leak_correction, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
                _clamp(prior[3] * leak_correction, _MIN_MULTIPLIER, _MAX_MULTIPLIER),
            )

    return {
        team.id: TeamRating(
            team_id=team.id,
            short_name=team.short_name,
            matches_played=played_count[team.id],
            effective_matches=effective[team.id],
            credibility=credibility[team.id],
            attack_home=attack[team.id][0],
            attack_away=attack[team.id][1],
            leak_home=leak[team.id][0],
            leak_away=leak[team.id][1],
            prior_attack_home=priors[team.id][0],
            prior_leak_home=priors[team.id][2],
        )
        for team in teams
    }


def _blend_goals(goals: float, expected_goals: float | None) -> float:
    """What a side's attack produced in one match, on the evidence available.

    Falls back to the scoreline when there's no xG for the fixture, which is
    the case for any match whose player stats haven't been ingested.
    """
    if expected_goals is None:
        return goals
    return XG_WEIGHT * expected_goals + (1 - XG_WEIGHT) * goals


def _correction(totals: list[float], credibility: float) -> float:
    """(observed / expected) ** credibility, guarded and clamped.

    The exponent is the damping: a team with no record gets 1.0 exactly, so
    their prior survives the pass untouched.
    """
    observed, expected = totals
    if credibility <= 0 or expected <= 0:
        return 1.0
    ratio = max(observed, 1e-6) / expected
    return _clamp(math.pow(ratio, credibility), _MIN_MULTIPLIER, _MAX_MULTIPLIER)


def rates_for_fixture(
    fixture: Fixture, ratings: dict[int, TeamRating]
) -> tuple[float, float] | None:
    """Expected goals for (home, away) in one fixture, or None if either side
    has no rating (which only happens with a half-ingested database)."""
    home = ratings.get(fixture.team_h_id)
    away = ratings.get(fixture.team_a_id)
    if home is None or away is None:
        return None
    lambda_home = BASE_GOALS_HOME * home.attack_home * away.leak_away
    lambda_away = BASE_GOALS_AWAY * away.attack_away * home.leak_home
    return (
        _clamp(lambda_home, _MIN_LAMBDA, _MAX_LAMBDA),
        _clamp(lambda_away, _MIN_LAMBDA, _MAX_LAMBDA),
    )


def compute_fixture_rates(
    session: Session, ratings: dict[int, TeamRating] | None = None
) -> dict[int, FixtureRates]:
    """Expected goals for every fixture in the database, keyed by fixture id."""
    ratings = compute_team_ratings(session) if ratings is None else ratings
    rates = {}
    for fixture in session.query(Fixture).all():
        pair = rates_for_fixture(fixture, ratings)
        if pair is None:
            continue
        rates[fixture.id] = FixtureRates(
            fixture_id=fixture.id,
            event=fixture.event,
            home_team_id=fixture.team_h_id,
            away_team_id=fixture.team_a_id,
            lambda_home=pair[0],
            lambda_away=pair[1],
        )
    return rates
