"""How a team's goals get shared out among its players.

`fplquant.engine.rates` says how many goals a side is expected to score. This
module says who scores them. The two together are a top-down model: a player's
expected goals are their *share* of their club's expected goals in that
specific fixture, not a rate estimated in isolation and then nudged for the
opponent.

That structure buys two things. It is internally consistent — sum the expected
goals of a club's players for a fixture and you get the club's expected goals
back, which a bottom-up model has no reason to satisfy and generally doesn't.
And it makes fixture difficulty propagate through the roster automatically: a
hard away tie lowers the club's goal rate, and every attacker's expected return
falls with it, in proportion to how much of the attack they are.

Shares themselves come from per-90 rates that are heavily shrunk early on,
toward a prior taken from the player's *price*. Price is the one estimate of a
player's attacking output that exists before a ball is kicked, it is continuous
where FPL's other preseason fields are missing or coarse, and it is the market's
aggregated view rather than this model's guess — the same reasoning that makes
a stock's price the default estimate of its value.
"""

import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.engine.minutes import compute_minutes_profiles
from fplquant.engine.scoring import PlayerFixtureInputs
from fplquant.models.orm import Player
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER

# Minutes of evidence before a player's own per-90 rate is trusted as much as
# their price-implied prior. Six full matches, matching the six appearances
# `fplquant.form.scoring` uses for points form — measured in minutes rather
# than appearances because a rate estimated off three substitute cameos
# deserves far less weight than one off three full matches, and an appearance
# count cannot tell those apart.
RATE_CREDIBILITY_MINUTES = 540.0
# Appearances before a player's own bonus rate is trusted as much as the prior.
BONUS_CREDIBILITY_APPEARANCES = 6.0

# League-typical per-90 goal and assist rates by position, for a player of
# exactly average price for that position. The price term below scales these;
# because shares are normalised within a club, the absolute level of these
# constants cancels out and only their ratios across positions matter.
POSITION_GOALS_PER_90: dict[int, float] = {
    GOALKEEPER: 0.001,
    DEFENDER: 0.05,
    MIDFIELDER: 0.13,
    FORWARD: 0.33,
}
POSITION_ASSISTS_PER_90: dict[int, float] = {
    GOALKEEPER: 0.005,
    DEFENDER: 0.05,
    MIDFIELDER: 0.11,
    FORWARD: 0.12,
}

# How steeply price maps to expected output. One means a player priced at twice
# their position's average is expected to return at twice the rate, which is
# roughly what the price ladder implies at the top end. The clamp stops a
# £15m striker's prior running away and, more importantly, stops a £3.9m
# fourth-choice defender's prior collapsing to nothing.
PRICE_ELASTICITY = 1.0
_MIN_PRICE_INDEX = 0.35
_MAX_PRICE_INDEX = 3.0

# Roughly three in four Premier League goals are assisted; the rest are solo
# efforts, penalties, rebounds and own goals, which pay nobody an assist.
ASSISTED_GOAL_FRACTION = 0.74

# Bonus points are dominated by goal involvements — the BPS formula rewards
# them heavily — so a player with no bonus history is credited a share of the
# bonus their expected involvements imply, plus a small base for the defensive
# and passing components everyone accrues.
BONUS_BASE = 0.06
BONUS_PER_INVOLVEMENT = 0.75


@dataclass(frozen=True)
class PlayerUsage:
    """A player's role in their side's attack, independent of any one fixture."""

    player_id: int
    web_name: str
    team_id: int
    element_type: int
    now_cost: int
    minutes_played: int
    rate_credibility: float  # 0.0 (pure price prior) to 1.0 (pure observed rate)
    goals_per_90: float
    assists_per_90: float
    p_start: float  # absolute: fitness news and rotation both folded in
    p_bench_appearance: float
    expected_minutes: float
    goal_share: float  # of their club's goals in a match, 0.0-1.0
    assist_share: float
    bonus_per_appearance: float


def _price_index(player: Player, position_mean_cost: dict[int, float]) -> float:
    mean_cost = position_mean_cost.get(player.element_type, 0.0)
    if mean_cost <= 0:
        return 1.0
    index = float((player.now_cost / mean_cost) ** PRICE_ELASTICITY)
    return max(_MIN_PRICE_INDEX, min(_MAX_PRICE_INDEX, index))


def compute_player_usage(session: Session) -> dict[int, PlayerUsage]:
    """Per-player attacking shares and minutes expectations, keyed by player id.

    Shares are normalised within each club, so they answer "what fraction of
    this side's goals is this player expected to score" and sum to 1 across the
    club. A player with no minutes expectation — injured, suspended, or simply
    never picked — takes a share of zero, and the rest of the squad absorbs it.
    """
    players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()
    if not players:
        return {}

    minutes_by_player = compute_minutes_profiles(session)
    position_mean_cost = {
        position: statistics.fmean([p.now_cost for p in players if p.element_type == position])
        for position in (GOALKEEPER, DEFENDER, MIDFIELDER, FORWARD)
        if any(p.element_type == position for p in players)
    }

    raw: dict[int, PlayerUsage] = {}
    goal_weight_by_team: dict[int, float] = {}
    assist_weight_by_team: dict[int, float] = {}

    for player in players:
        stats = player.gameweek_stats
        minutes = sum(s.minutes for s in stats)
        appearances = sum(1 for s in stats if s.minutes > 0)

        credibility = minutes / (minutes + RATE_CREDIBILITY_MINUTES)
        price_index = _price_index(player, position_mean_cost)
        prior_goals = POSITION_GOALS_PER_90[player.element_type] * price_index
        prior_assists = POSITION_ASSISTS_PER_90[player.element_type] * price_index

        if minutes > 0:
            observed_goals = 90 * sum(s.expected_goals for s in stats) / minutes
            observed_assists = 90 * sum(s.expected_assists for s in stats) / minutes
        else:
            observed_goals = observed_assists = 0.0

        goals_per_90 = credibility * observed_goals + (1 - credibility) * prior_goals
        assists_per_90 = credibility * observed_assists + (1 - credibility) * prior_assists

        profile = minutes_by_player.get(player.id)
        p_start = profile.p_start if profile else 0.0
        p_bench = profile.p_bench_appearance if profile else 0.0
        expected_minutes = profile.expected_minutes if profile else 0.0

        bonus_weight = appearances / (appearances + BONUS_CREDIBILITY_APPEARANCES)
        observed_bonus = sum(s.bonus for s in stats) / appearances if appearances else 0.0
        prior_bonus = BONUS_BASE + BONUS_PER_INVOLVEMENT * (goals_per_90 + assists_per_90)
        bonus = bonus_weight * observed_bonus + (1 - bonus_weight) * prior_bonus

        minutes_share = expected_minutes / 90
        goal_weight = minutes_share * goals_per_90
        assist_weight = minutes_share * assists_per_90
        goal_weight_by_team[player.team_id] = (
            goal_weight_by_team.get(player.team_id, 0.0) + goal_weight
        )
        assist_weight_by_team[player.team_id] = (
            assist_weight_by_team.get(player.team_id, 0.0) + assist_weight
        )

        raw[player.id] = PlayerUsage(
            player_id=player.id,
            web_name=player.web_name,
            team_id=player.team_id,
            element_type=player.element_type,
            now_cost=player.now_cost,
            minutes_played=minutes,
            rate_credibility=credibility,
            goals_per_90=goals_per_90,
            assists_per_90=assists_per_90,
            p_start=p_start,
            p_bench_appearance=p_bench,
            expected_minutes=expected_minutes,
            goal_share=goal_weight,  # replaced below, once the team total is known
            assist_share=assist_weight,
            bonus_per_appearance=bonus,
        )

    usage = {}
    for player_id, entry in raw.items():
        goal_total = goal_weight_by_team.get(entry.team_id, 0.0)
        assist_total = assist_weight_by_team.get(entry.team_id, 0.0)
        usage[player_id] = PlayerUsage(
            **{
                **entry.__dict__,
                "goal_share": entry.goal_share / goal_total if goal_total > 0 else 0.0,
                "assist_share": entry.assist_share / assist_total if assist_total > 0 else 0.0,
            }
        )
    return usage


def fixture_inputs(
    usage: PlayerUsage, lambda_for: float, lambda_against: float
) -> PlayerFixtureInputs:
    """Turn a player's usage plus one fixture's goal rates into scoring inputs.

    This is the join between the two halves of the model: `lambda_for` is what
    `fplquant.engine.rates` expects the player's side to score, and the shares
    decide how much of it lands on this player.
    """
    return PlayerFixtureInputs(
        element_type=usage.element_type,
        p_start=usage.p_start,
        p_bench_appearance=usage.p_bench_appearance,
        expected_minutes=usage.expected_minutes,
        expected_goals=lambda_for * usage.goal_share,
        expected_assists=lambda_for * ASSISTED_GOAL_FRACTION * usage.assist_share,
        lambda_conceded=lambda_against,
        expected_bonus=usage.bonus_per_appearance,
    )
