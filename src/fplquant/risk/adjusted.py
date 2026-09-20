from dataclasses import dataclass

from sqlalchemy.orm import Session

from fplquant.form.fixtures import compute_fixture_adjusted_scores
from fplquant.market.volatility import compute_volatility_scores
from fplquant.models.orm import Player
from fplquant.risk.injury import compute_injury_risk_scores


@dataclass(frozen=True)
class RiskAdjustedScore:
    player_id: int
    web_name: str
    expected_points: float
    coefficient_of_variation: float
    injury_risk_pct: float
    volatility_penalty: float
    availability_factor: float
    risk_adjusted_points: float


def compute_risk_adjusted_scores(
    session: Session,
    halflife: float = 3.0,
    risk_aversion: float = 1.0,
    injury_weight: float = 1.0,
    expected_points: dict[int, float] | None = None,
) -> list[RiskAdjustedScore]:
    """Blend expected points, week-to-week volatility, and injury risk into a
    single risk-adjusted expected-points metric — a Sharpe-ratio-style signal:
    reward for predicted return, penalized by risk.

        risk_adjusted_points = expected_points
                                * (1 - injury_weight * injury_risk_pct / 100)
                                / (1 + risk_aversion * coefficient_of_variation)

    `coefficient_of_variation` (points_stdev / points_mean, from the market
    module) is used rather than raw stdev so the volatility penalty scales
    with a player's own point level, not just their absolute variance — a
    tight range around a big total isn't penalized like a tight range around
    a tiny one would misleadingly avoid. `injury_risk_pct` is the module 4
    blended risk score (age/position/history/status), always available
    (unlike volatility, it needs no gameweek history).

    With no gameweek history yet (e.g. preseason), volatility is undefined
    for everyone, so the volatility term degrades to a neutral 1.0 (no
    penalty) rather than leaving the metric undefined.

    `expected_points` defaults to the fixture-adjusted EWMA (opponent
    strength, venue, and the chance the player plays their next match — see
    `fplquant.form.fixtures.compute_fixture_adjusted_scores`), so
    `injury_risk_pct` on top of that is a separate, longer-run signal: how
    injury-prone this player generally is, rather than whether they're
    literally out for the very next match. A caller may pass its own
    projection instead, keyed by player id — the risk arithmetic is the same
    whatever produced the number it is applied to, and the optimizer's engine
    path does exactly that rather than reimplementing the penalties.
    """
    if expected_points is None:
        expected_points = {
            s.player_id: s.adjusted_points
            for s in compute_fixture_adjusted_scores(session, halflife)
        }
    cov_by_player = {
        s.player_id: s.coefficient_of_variation or 0.0
        for s in compute_volatility_scores(session)
        if s.coefficient_of_variation is not None
    }
    injury_risk_by_player = {s.player_id: s.risk_pct for s in compute_injury_risk_scores(session)}

    scores = []
    for player in session.query(Player).all():
        points = expected_points.get(player.id, 0.0)
        cov = cov_by_player.get(player.id, 0.0)
        injury_pct = injury_risk_by_player.get(player.id, 0.0)

        volatility_penalty = 1 + risk_aversion * abs(cov)
        availability_factor = max(0.0, 1 - injury_weight * injury_pct / 100)
        risk_adjusted_points = points * availability_factor / volatility_penalty

        scores.append(
            RiskAdjustedScore(
                player_id=player.id,
                web_name=player.web_name,
                expected_points=points,
                coefficient_of_variation=cov,
                injury_risk_pct=injury_pct,
                volatility_penalty=volatility_penalty,
                availability_factor=availability_factor,
                risk_adjusted_points=risk_adjusted_points,
            )
        )
    return sorted(scores, key=lambda s: s.risk_adjusted_points, reverse=True)
