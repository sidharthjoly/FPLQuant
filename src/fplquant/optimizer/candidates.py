import logging

from sqlalchemy.orm import Session, selectinload

from fplquant.engine.horizon import (
    DEFAULT_DECAY,
    DEFAULT_HORIZON,
    HorizonProjection,
    project_horizon,
)
from fplquant.form.fixtures import (
    FixtureAdjustedScore,
    chance_of_playing,
    compute_fixture_adjusted_scores,
)
from fplquant.models.orm import Player
from fplquant.optimizer.multiperiod import HorizonCandidate
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER, PlayerCandidate
from fplquant.risk.adjusted import compute_risk_adjusted_scores
from fplquant.schedule import upcoming_events

logger = logging.getLogger(__name__)

UNAVAILABLE_STATUSES = {"u"}  # unavailable (e.g. left the club / not in FPL this season)

# Which projection answers "how many points next match".
#
# FORM is an EWMA of recent points adjusted for opponent and venue. ENGINE is
# the structural model — fitted goal rates, usage shares, the scoring table,
# the minutes model — read for the next event only.
#
# They are not equally good at the question, and the difference is measured
# rather than assumed. Replaying 26/27 with `fplquant-backtest --lineups`, the
# same integer program under the same constraints scored 39 points more over
# GW2-4 on ENGINE — thirteen a week — and in GW1 the FORM path collapses
# outright: with no history to average it projects near zero for everybody,
# leaves £29.5m of the budget unspent because nothing looks worth buying, and
# returns 10 points with nine of eleven starters blanking.
#
# The library default stays FORM so that nothing changes meaning underneath a
# caller that has not thought about it; the API, the CLI and the planner all
# ask for ENGINE explicitly.
FORM = "form"
ENGINE = "engine"


def _candidates_from_points(
    session: Session,
    points_by_player: dict[int, float],
    exclude_unavailable: bool,
    fixtures_by_player: dict[int, FixtureAdjustedScore] | None = None,
    start_probability: dict[int, float] | None = None,
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
                # Selection odds, and only the engine models them. None means
                # "not modelled here", not "zero" — see `PlayerCandidate`.
                start_probability=(start_probability or {}).get(player.id),
            )
        )
    return candidates


def next_event_points(session: Session) -> tuple[dict[int, float], dict[int, float]]:
    """The engine's projection for the next gameweek, and its start odds.

    Asked for a single event and read for that event alone. `discounted_points`
    on the same projection is a multi-week aggregate, and ranking a one-week
    squad by it would treat a player whose club blanks next weekend as though
    he were playing.

    Empty when there is no upcoming fixture at all — an out-of-season pool, or
    a database that has not ingested a fixture list yet. Callers fall back to
    the form path rather than handing the solver a pool of zeros.
    """
    points: dict[int, float] = {}
    starts: dict[int, float] = {}
    for projection in project_horizon(session, horizon=1):
        event = next(iter(projection.points_by_event), None)
        if event is None:
            continue
        points[projection.player_id] = projection.points_by_event[event]
        starts[projection.player_id] = projection.usage.p_start
    return points, starts


def build_candidates_from_db(
    session: Session,
    halflife: float = 3.0,
    exclude_unavailable: bool = True,
    projection: str = FORM,
) -> list[PlayerCandidate]:
    """Build optimizer input from the database, maximizing expected points for
    each player's next match.

    `projection` picks where that expectation comes from — see `FORM` and
    `ENGINE` above for which to want and why. Either way the fixture the
    number refers to, its difficulty and the player's chance of being fit for
    it come from `form.fixtures`, so the two paths describe the same match and
    disagree only about how many points it is worth.

    For a risk-adjusted alternative, see
    `fplquant.optimizer.candidates.build_risk_adjusted_candidates_from_db`.
    """
    fixtures_by_player = {
        s.player_id: s for s in compute_fixture_adjusted_scores(session, halflife)
    }
    points_by_player, start_probability = next_match_points(
        session, projection, fixtures_by_player, halflife
    )
    return _candidates_from_points(
        session, points_by_player, exclude_unavailable, fixtures_by_player, start_probability
    )


def next_match_points(
    session: Session,
    projection: str,
    fixtures_by_player: dict[int, FixtureAdjustedScore] | None = None,
    halflife: float = 3.0,
) -> tuple[dict[int, float], dict[int, float]]:
    """Next-match points by player, from whichever projection was asked for.

    Public because a squad and the pool it is compared against have to be
    priced by the same projection. They were not, briefly: the pool moved to
    the engine while `transfers.team_lookup` still valued the players a
    manager owns on the form EWMA, whose numbers run roughly twice as high.
    Nothing could out-score an incumbent on that scale, so every team in the
    game was told to make no transfers, with a gain of exactly zero and no
    error anywhere.

    An engine projection that comes back empty falls back to form rather than
    failing. There is exactly one way for that to happen — no upcoming
    fixture to project — and in that case the form path is no worse, while an
    empty pool would turn a season break into a 500.
    """
    if projection not in (FORM, ENGINE):
        raise ValueError(f"Unknown projection {projection!r}; expected {FORM!r} or {ENGINE!r}")
    if fixtures_by_player is None:
        fixtures_by_player = {
            s.player_id: s for s in compute_fixture_adjusted_scores(session, halflife)
        }
    if projection == ENGINE:
        points, starts = next_event_points(session)
        if points:
            return points, starts
        logger.warning(
            "The engine projected no upcoming gameweek, so there is nothing for it to "
            "rank; falling back to the form projection."
        )
    return {pid: s.adjusted_points for pid, s in fixtures_by_player.items()}, {}


def build_risk_adjusted_candidates_from_db(
    session: Session,
    halflife: float = 3.0,
    risk_aversion: float = 1.0,
    injury_weight: float = 1.0,
    exclude_unavailable: bool = True,
    projection: str = FORM,
) -> list[PlayerCandidate]:
    """Build optimizer input maximizing risk-adjusted expected points instead
    of raw predicted points — see `fplquant.risk.adjusted.compute_risk_adjusted_scores`
    for how volatility and injury risk are folded in.

    `projection` chooses the expectation the penalties are applied *to*, the
    same choice `build_candidates_from_db` offers. The volatility and injury
    terms are unchanged by it: they scale whatever number they are given, so
    there is one implementation of the risk arithmetic rather than one per
    projection.
    """
    fixtures_by_player = {
        s.player_id: s for s in compute_fixture_adjusted_scores(session, halflife)
    }
    expected_points, start_probability = next_match_points(
        session, projection, fixtures_by_player, halflife
    )
    points_by_player = {
        s.player_id: s.risk_adjusted_points
        for s in compute_risk_adjusted_scores(
            session,
            halflife,
            risk_aversion,
            injury_weight,
            expected_points=expected_points,
        )
    }
    return _candidates_from_points(
        session, points_by_player, exclude_unavailable, fixtures_by_player, start_probability
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
    players_by_id = {p.id: p for p in session.query(Player).all()}

    kept: list[HorizonProjection] = []
    counts: dict[int, int] = {}
    for projection in projections:  # already sorted by discounted points, best first
        forced = projection.player_id in always_include
        if not forced:
            player = players_by_id.get(projection.player_id)
            if exclude_unavailable and player is not None and player.status in UNAVAILABLE_STATUSES:
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
                # `p.usage.p_start` is selection odds, not fitness. It used to be
                # assigned to `chance_of_playing`, which the UI renders as
                # "N% to play" — so a fully fit Haaland read as "56% to play".
                # Fitness comes from the same helper the next-match path uses, and
                # refers to the same fixture `_first_opponent` names.
                chance_of_playing=(
                    chance_of_playing(players_by_id[p.player_id])
                    if p.player_id in players_by_id
                    else 1.0
                ),
                start_probability=p.usage.p_start,
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
