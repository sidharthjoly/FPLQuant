import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.models.orm import Player


@dataclass(frozen=True)
class VolatilityScore:
    player_id: int
    web_name: str
    gameweeks_considered: int
    points_mean: float
    points_stdev: float
    coefficient_of_variation: float | None  # stdev / mean; None when mean is 0 (undefined)


def compute_volatility(web_name: str, player_id: int, points: list[int]) -> VolatilityScore | None:
    """Population standard deviation of weekly points — the direct analog of
    return volatility in finance. `coefficient_of_variation` (stdev/mean)
    additionally normalizes for scale, so a volatile bench player and a
    volatile premium forward are comparable. Returns None with fewer than 2
    gameweeks, since variance is undefined for a single point.
    """
    if len(points) < 2:
        return None
    mean = statistics.fmean(points)
    stdev = statistics.pstdev(points)
    return VolatilityScore(
        player_id=player_id,
        web_name=web_name,
        gameweeks_considered=len(points),
        points_mean=mean,
        points_stdev=stdev,
        coefficient_of_variation=(stdev / mean) if mean != 0 else None,
    )


def compute_volatility_scores(session: Session) -> list[VolatilityScore]:
    players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()
    scores = []
    for player in players:
        # Summed per round, not per row. A gameweek is the unit a manager
        # actually scores in, and in a double it is two matches — so the
        # fourteen points from a double belong in the series as one 14, not as
        # two sevens, which would understate exactly the weeks with the most
        # variance in them.
        by_round: dict[int, int] = {}
        for stat in player.gameweek_stats:
            by_round[stat.round] = by_round.get(stat.round, 0) + stat.total_points
        points = [by_round[r] for r in sorted(by_round)]
        score = compute_volatility(player.web_name, player.id, points)
        if score is not None:
            scores.append(score)
    return sorted(scores, key=lambda s: s.points_stdev, reverse=True)
