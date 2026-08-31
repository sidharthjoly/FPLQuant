import statistics
from dataclasses import dataclass
from itertools import combinations

from sqlalchemy.orm import Session, selectinload

from fplquant.models.orm import Player


@dataclass(frozen=True)
class TeammateCorrelation:
    team_id: int
    player_a_id: int
    player_a_web_name: str
    player_b_id: int
    player_b_web_name: str
    overlap_gameweeks: int
    correlation: float


def _points_by_round(player: Player) -> dict[int, int]:
    """A player's points per gameweek, summed across a double's two fixtures.

    Summed rather than indexed: a dict comprehension keyed on the round would
    keep whichever of a double gameweek's two rows happened to iterate last and
    silently drop the other, which is the same class of bug that used to lose
    the row entirely at ingest.
    """
    totals: dict[int, int] = {}
    for stat in player.gameweek_stats:
        totals[stat.round] = totals.get(stat.round, 0) + stat.total_points
    return totals


def _aligned_points(player_a: Player, player_b: Player) -> tuple[list[int], list[int]]:
    points_a = _points_by_round(player_a)
    points_b = _points_by_round(player_b)
    common_rounds = sorted(set(points_a) & set(points_b))
    return [points_a[r] for r in common_rounds], [points_b[r] for r in common_rounds]


def compute_teammate_correlations(
    session: Session, min_overlap: int = 3
) -> list[TeammateCorrelation]:
    """Pairwise correlation of weekly points between teammates.

    A diversification signal: two teammates whose points move together
    (e.g. a striker and the midfielder who mainly assists him) add less
    variety to a squad than two whose returns are independent or opposed —
    the same logic as correlated assets in a portfolio. Pairs with fewer than
    `min_overlap` shared gameweeks, or with a constant points series (zero
    variance — correlation is undefined), are skipped.
    """
    players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()
    by_team: dict[int, list[Player]] = {}
    for player in players:
        by_team.setdefault(player.team_id, []).append(player)

    results = []
    for team_id, team_players in by_team.items():
        for player_a, player_b in combinations(team_players, 2):
            points_a, points_b = _aligned_points(player_a, player_b)
            if len(points_a) < min_overlap:
                continue
            try:
                correlation = statistics.correlation(points_a, points_b)
            except statistics.StatisticsError:
                continue  # zero variance in one series — undefined
            results.append(
                TeammateCorrelation(
                    team_id=team_id,
                    player_a_id=player_a.id,
                    player_a_web_name=player_a.web_name,
                    player_b_id=player_b.id,
                    player_b_web_name=player_b.web_name,
                    overlap_gameweeks=len(points_a),
                    correlation=correlation,
                )
            )
    return sorted(results, key=lambda r: r.correlation, reverse=True)
