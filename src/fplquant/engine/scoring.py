"""FPL's scoring rules, applied to a Poisson model of a single fixture.

This module is deliberately pure: no database, no session, no I/O. It takes a
description of what a player is expected to do in one match and turns it into
points using FPL's published scoring table. Everything that decides *what* to
expect lives in `fplquant.engine.rates` (team goal rates) and
`fplquant.engine.usage` (how a team's goals are shared out among its players).

Why go through the scoring table at all, when `fplquant.form.scoring` already
predicts points directly from a player's past points? Because points are a
*consequence*, not a quantity in their own right. A defender's expected points
are dominated by the probability their side keeps a clean sheet, which is a
property of the opponent's attack and nothing to do with the defender's own
scoring history. Modelling the components separately means a fixture swing
moves the parts it should move — a hard away tie collapses a defender's clean
sheet term while barely touching a forward's appearance points — instead of
scaling every player's whole score by one blunt multiplier.

It also produces a *distribution*, not just a mean, which is what
`fplquant.engine.simulate` needs and what a mean-only model can never give you:
two players with the same expected points can have completely different odds of
returning a haul, and captaincy is a bet on the upside, not on the mean.
"""

import math
from dataclasses import dataclass

from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER

# --- FPL's scoring table, as data ---------------------------------------------
# Kept as tables rather than inlined arithmetic so a rule change (FPL has
# revised these several times) is a one-line edit in one place.

APPEARANCE_POINTS = 1  # for playing at all...
SIXTY_MINUTE_POINTS = 1  # ...plus this again for playing 60 minutes or more
ASSIST_POINTS = 3
GOAL_POINTS: dict[int, int] = {GOALKEEPER: 6, DEFENDER: 6, MIDFIELDER: 5, FORWARD: 4}
CLEAN_SHEET_POINTS: dict[int, int] = {GOALKEEPER: 4, DEFENDER: 4, MIDFIELDER: 1, FORWARD: 0}
# Goalkeepers and defenders lose a point for every second goal their team
# concedes while they're on the pitch.
GOALS_CONCEDED_PER_PENALTY = 2
CONCEDED_PENALTY_POSITIONS = frozenset({GOALKEEPER, DEFENDER})
# Goalkeepers gain a point for every third save.
SAVES_PER_POINT = 3

# Positions whose clean-sheet reward is worth modelling at all. Forwards get
# nothing for one, so the term is skipped for them rather than multiplied by 0.
CLEAN_SHEET_POSITIONS = frozenset({GOALKEEPER, DEFENDER, MIDFIELDER})

# --- League-typical constants, used where the FPL API gives us no data ---------

# The ORM stores no card counts, so disciplinary points are charged at a flat
# league-typical rate per 90 minutes rather than per player. Centre-backs and
# holding midfielders are booked more often than keepers and forwards, hence
# the split. These are small numbers by design: getting them individually right
# would move a ranking by a fraction of a point, and pretending to know a
# specific player's booking rate from a handful of matches would not.
CARD_POINTS_PER_90: dict[int, float] = {
    GOALKEEPER: -0.06,
    DEFENDER: -0.18,
    MIDFIELDER: -0.16,
    FORWARD: -0.13,
}

# Saves aren't in the ORM either, but they're tightly coupled to something we
# do model: a keeper facing a dangerous side makes more saves as well as
# conceding more. Premier League keepers face roughly three shots on target for
# every goal they concede, so saves run at about twice the goals-conceded rate.
SAVES_PER_GOAL_CONCEDED = 2.1

# A starter who is on the pitch at kickoff still occasionally comes off before
# the hour — injury, a red card, a blowout. Empirically a little over one start
# in ten ends before 60 minutes.
START_LASTS_SIXTY = 0.88

# Beyond this many goals in one match the Poisson tail contributes less than a
# rounding error to any expectation we compute, so series over `k` stop here.
_MAX_GOALS = 12


@dataclass(frozen=True)
class PlayerFixtureInputs:
    """Everything the scoring table needs to know about one player in one match.

    All the expectations here are *unconditional* — already multiplied through
    by how likely the player is to be on the pitch — because that's the form
    `fplquant.engine.usage` produces them in, and keeping the conditioning in
    one place stops it being applied twice.
    """

    element_type: int
    p_start: float  # probability of being named in the starting XI
    p_bench_appearance: float  # probability of appearing given they don't start
    expected_minutes: float
    expected_goals: float  # this player's share of their side's expected goals
    expected_assists: float
    lambda_conceded: float  # their side's expected goals *against* in this fixture
    expected_bonus: float  # bonus points, conditional on appearing

    @property
    def p_appear(self) -> float:
        return self.p_start + (1 - self.p_start) * self.p_bench_appearance

    @property
    def p_sixty(self) -> float:
        """Probability of reaching 60 minutes — effectively, of starting and
        staying on. A substitute almost never comes on early enough to reach
        the hour mark, so bench appearances are not counted toward it."""
        return self.p_start * START_LASTS_SIXTY


@dataclass(frozen=True)
class PointsBreakdown:
    """Expected points for one player in one match, split by scoring rule.

    The split is the point of this dataclass as much as the total is: it turns
    "the model likes this defender" into "the model likes this defender because
    it gives them a 42% clean sheet chance", which is checkable by a human and,
    unlike a single number, wrong in a visible way when it's wrong.
    """

    appearance: float
    goals: float
    assists: float
    clean_sheet: float
    goals_conceded: float  # negative
    saves: float
    bonus: float
    cards: float  # negative
    clean_sheet_probability: float
    total: float


def poisson_pmf(lam: float, k: int) -> float:
    """P(K = k) for K ~ Poisson(lam).

    Computed in log space via `lgamma` so large `k` doesn't overflow the
    factorial. numpy is a dependency here, but a scalar pmf through numpy is
    slower than the arithmetic itself; the vectorised paths live in
    `fplquant.engine.simulate`.
    """
    if lam < 0:
        raise ValueError("lam must be non-negative")
    if k < 0:
        return 0.0
    if lam == 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def clean_sheet_probability(lambda_conceded: float) -> float:
    """P(the opponent fails to score), i.e. P(K = 0) for K ~ Poisson(lambda)."""
    return math.exp(-lambda_conceded)


def expected_step_count(lam: float, per: int) -> float:
    """E[floor(K / per)] for K ~ Poisson(lam).

    FPL's goals-conceded penalty and saves reward are both step functions —
    one point per two conceded, one per three saves — and E[floor(K/n)] is not
    floor(E[K]/n). At lambda = 1.5 the naive version says a defender loses zero
    points, when in truth they lose about half of one. The difference is small
    per player and systematic across every defender and keeper in the pool,
    which is exactly the kind of bias that quietly reorders a ranking.
    """
    if per < 1:
        raise ValueError("per must be at least 1")
    return sum(poisson_pmf(lam, k) * (k // per) for k in range(_MAX_GOALS + 1))


def expected_points(inputs: PlayerFixtureInputs) -> PointsBreakdown:
    """Expected FPL points for one player in one match, rule by rule."""
    position = inputs.element_type
    p_appear = inputs.p_appear
    p_sixty = inputs.p_sixty
    minutes_share = inputs.expected_minutes / 90

    appearance = p_appear * APPEARANCE_POINTS + p_sixty * SIXTY_MINUTE_POINTS
    goals = inputs.expected_goals * GOAL_POINTS[position]
    assists = inputs.expected_assists * ASSIST_POINTS

    cs_probability = clean_sheet_probability(inputs.lambda_conceded)
    clean_sheet = 0.0
    if position in CLEAN_SHEET_POSITIONS:
        # The clean sheet has to survive to the 60th minute *and* the player
        # has to still be on the pitch for it, which is why this is gated on
        # p_sixty rather than p_appear.
        clean_sheet = p_sixty * cs_probability * CLEAN_SHEET_POINTS[position]

    goals_conceded = 0.0
    if position in CONCEDED_PENALTY_POSITIONS:
        # Charged over the minutes actually played rather than gated on 60,
        # since the penalty accrues from the first minute — a keeper who plays
        # a full match against a strong side is exposed to all of it.
        goals_conceded = -expected_step_count(
            inputs.lambda_conceded, GOALS_CONCEDED_PER_PENALTY
        ) * min(1.0, minutes_share)

    saves = 0.0
    if position == GOALKEEPER:
        expected_saves = SAVES_PER_GOAL_CONCEDED * inputs.lambda_conceded
        saves = expected_step_count(expected_saves, SAVES_PER_POINT) * p_sixty

    bonus = inputs.expected_bonus * p_appear
    cards = CARD_POINTS_PER_90[position] * minutes_share

    total = appearance + goals + assists + clean_sheet + goals_conceded + saves + bonus + cards
    return PointsBreakdown(
        appearance=appearance,
        goals=goals,
        assists=assists,
        clean_sheet=clean_sheet,
        goals_conceded=goals_conceded,
        saves=saves,
        bonus=bonus,
        cards=cards,
        clean_sheet_probability=cs_probability,
        total=total,
    )
