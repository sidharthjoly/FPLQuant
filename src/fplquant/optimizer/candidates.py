from sqlalchemy.orm import Session, selectinload

from fplquant.engine.horizon import (
    DEFAULT_DECAY,
    DEFAULT_HORIZON,
    HorizonProjection,
    project_horizon,
)
from fplquant.form.fixtures import FixtureAdjustedScore, compute_fixture_adjusted_scores
from fplquant.models.orm import Player
from fplquant.optimizer.multiperiod import HorizonCandidate
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER, PlayerCandidate
from fplquant.risk.adjusted import compute_risk_adjusted_scores
from fplquant.schedule import upcoming_events

UNAVAILABLE_STATUSES = {"u"}  # unavailable (e.g. left the club / not in FPL this season)


def _candidates_from_points(
    session: Session,
    points_by_player: dict[int, float],
    exclude_unavailable: bool,
    fixtures_by_player: dict[int, FixtureAdjustedScore] | None = None,
) -> list[PlayerCandidate]:
    players = session.query(Player).options(selectinload(Player.team)).all()
    fixtures_by_player = fixtures_by_player or {}
    candidates = []
    for player in players:
        if exclude_unavailable and player.status in UNAVAILABLE_STATUSES:
            continue
        fixture = fixtures_by_player.get(player.id)
        candidates.append(
            PlayerCandidate(
                player_id=player.id,
                web_name=player.web_name,
                team_id=player.team_id,
                team_short_name=player.team.short_name,
                element_type=player.element_type,
                now_cost=player.now_cost,
                predicted_points=points_by_player.get(player.id, 0.0),
                next_opponent=fixture.opponent_short_name if fixture else None,
                next_opponent_is_home=fixture.is_home if fixture else None,
                fixture_difficulty=fixture.difficulty if fixture else None,
                chance_of_playing=fixture.chance_of_playing if fixture else 1.0,
            )
        )
    return candidates


def build_candidates_from_db(
    session: Session, halflife: float = 3.0, exclude_unavailable: bool = True
) -> list[PlayerCandidate]:
    """Build optimizer input from the database, maximizing fixture-adjusted
    expected points for each player's next match.

    See `fplquant.form.fixtures.compute_fixture_adjusted_scores` for how
    points are predicted: season-form EWMA, adjusted for opponent strength,
    home/away venue, and the chance the player actually plays. For a
    risk-adjusted alternative, see
    `fplquant.optimizer.candidates.build_risk_adjusted_candidates_from_db`.
    """
    fixtures_by_player = {
        s.player_id: s for s in compute_fixture_adjusted_scores(session, halflife)
    }
    points_by_player = {pid: s.adjusted_points for pid, s in fixtures_by_player.items()}
    return _candidates_from_points(
        session, points_by_player, exclude_unavailable, fixtures_by_player
    )


def build_risk_adjusted_candidates_from_db(
    session: Session,
    halflife: float = 3.0,
    risk_aversion: float = 1.0,
    injury_weight: float = 1.0,
    exclude_unavailable: bool = True,
) -> list[PlayerCandidate]:
    """Build optimizer input maximizing risk-adjusted expected points instead
    of raw predicted points — see `fplquant.risk.adjusted.compute_risk_adjusted_scores`
    for how volatility and injury risk are folded in.
    """
    fixtures_by_player = {
        s.player_id: s for s in compute_fixture_adjusted_scores(session, halflife)
    }
    points_by_player = {
        s.player_id: s.risk_adjusted_points
        for s in compute_risk_adjusted_scores(session, halflife, risk_aversion, injury_weight)
    }
    return _candidates_from_points(
        session, points_by_player, exclude_unavailable, fixtures_by_player
    )


# How many players per position the horizon planner considers. The multi-period
# program has several binaries per player *per gameweek*, so handing it all 600
# players the way the single-gameweek solver does turns a two-second solve into
# an intractable one. Trimming to the top of each position by projected points
# is safe in a way that trimming arbitrarily would not be: the excluded players
# are, by construction, ones the objective would never have picked. The counts
# are generous multiples of the squad requirements (2/5/5/3) so that budget and
# club-count constraints still have room to work around a blocked pick.
HORIZON_POOL_PER_POSITION: dict[int, int] = {
    GOALKEEPER: 12,
    DEFENDER: 45,
    MIDFIELDER: 50,
    FORWARD: 30,
}


def build_horizon_candidates_from_db(
    session: Session,
    horizon: int = DEFAULT_HORIZON,
    decay: float = DEFAULT_DECAY,
    exclude_unavailable: bool = True,
    always_include: set[int] | None = None,
    pool_per_position: dict[int, int] | None = None,
) -> tuple[list[HorizonCandidate], list[int]]:
    """Build multi-gameweek optimizer input, and the gameweeks it covers.

    Points come from `fplquant.engine.horizon`, so each candidate carries a
    per-gameweek estimate rather than a single number — which is what lets the
    planner see a double gameweek as a spike and a blank as a hole.

    `always_include` survives the pool trimming unconditionally. Players you
    already own must be in the pool whatever their projection says, or the
    program has no way to express keeping them and will "solve" by selling a
    squad it was never allowed to hold.
    """
    pool_per_position = pool_per_position or HORIZON_POOL_PER_POSITION
    always_include = always_include or set()

    projections = project_horizon(session, horizon=horizon, decay=decay)
    events = upcoming_events(session, horizon)
    statuses = {p.id: p.status for p in session.query(Player).all()}

    kept: list[HorizonProjection] = []
    counts: dict[int, int] = {}
    for projection in projections:  # already sorted by discounted points, best first
        forced = projection.player_id in always_include
        if not forced:
            if exclude_unavailable and statuses.get(projection.player_id) in UNAVAILABLE_STATUSES:
                continue
            limit = pool_per_position.get(projection.element_type, 0)
            if counts.get(projection.element_type, 0) >= limit:
                continue
            counts[projection.element_type] = counts.get(projection.element_type, 0) + 1
        kept.append(projection)

    candidates = [
        HorizonCandidate(
            candidate=PlayerCandidate(
                player_id=p.player_id,
                web_name=p.web_name,
                team_id=p.team_id,
                team_short_name=p.team_short_name,
                element_type=p.element_type,
                now_cost=p.now_cost,
                predicted_points=p.discounted_points,
                next_opponent=_first_opponent(p),
                next_opponent_is_home=_first_is_home(p),
                chance_of_playing=p.usage.p_start,
            ),
            points_by_event=p.points_by_event,
        )
        for p in kept
    ]
    return candidates, events


def _first_opponent(projection: HorizonProjection) -> str | None:
    for event in projection.events:
        for fixture in event.fixtures:
            return fixture.opponent_short_name
    return None


def _first_is_home(projection: HorizonProjection) -> bool | None:
    for event in projection.events:
        for fixture in event.fixtures:
            return fixture.is_home
    return None
