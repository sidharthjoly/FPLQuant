"""Multi-gameweek projection and planning endpoints.

These sit alongside `/optimize` rather than replacing it. `/optimize` answers
"what is the best squad for this weekend", which is a fast, cheap question;
`/projections` and `/plan` answer "what is the best squad for the next month,
and what should I do first", which is a slower and more useful one.
"""

import hashlib
import logging
from collections.abc import Mapping

import numpy as np
import numpy.typing as npt
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.orm import Session

from fplquant.api import schemas
from fplquant.api.cache import cache_get, cache_set
from fplquant.api.deps import get_session
from fplquant.config import settings
from fplquant.data.fpl_client import FPLClient
from fplquant.engine.horizon import (
    DEFAULT_DECAY,
    DEFAULT_HORIZON,
    HorizonProjection,
    project_horizon,
)
from fplquant.engine.simulate import DEFAULT_SIMULATIONS, simulate_event, summarize_player
from fplquant.optimizer.candidates import build_horizon_candidates_from_db
from fplquant.optimizer.multiperiod import GameweekPlan, plan_horizon
from fplquant.optimizer.types import SquadConstraints
from fplquant.transfers.team_lookup import TeamNotFoundError, fetch_current_squad

router = APIRouter(tags=["planner"])
logger = logging.getLogger(__name__)

# The Monte Carlo run is capped well below what the CLI allows. The API is a
# shared, cached service and the marginal precision past a few thousand runs is
# a rounding error on a percentile.
MAX_SIMULATIONS = 20000


@router.get("/projections", response_model=schemas.ProjectionResponse)
def projections(
    horizon: int = Query(default=DEFAULT_HORIZON, ge=1, le=10),
    decay: float = Query(default=DEFAULT_DECAY, gt=0, le=1),
    limit: int = Query(default=50, ge=1, le=600),
    simulate: bool = Query(default=False, description="Also Monte Carlo the next full gameweek"),
    simulations: int = Query(default=DEFAULT_SIMULATIONS, ge=100, le=MAX_SIMULATIONS),
    seed: int | None = Query(default=None, description="Seed the simulation for repeatability"),
    session: Session = Depends(get_session),
) -> schemas.ProjectionResponse:
    """Expected points per gameweek over a horizon, best first.

    Each player's response carries their fixtures round by round, the goal
    rates behind each one, and the scoring-rule breakdown that produced the
    number — see `fplquant.engine.scoring`. With `simulate=true` the next full
    gameweek is also sampled, adding a floor, a ceiling and haul odds.
    """
    cache_key = _key("projections", horizon, decay, limit, simulate, simulations, seed)
    cached = _cached(cache_key, schemas.ProjectionResponse)
    if cached is not None:
        return cached

    all_projections = project_horizon(session, horizon=horizon, decay=decay)
    events = [event.event for event in all_projections[0].events] if all_projections else []

    simulated_event = None
    samples: dict[int, npt.NDArray[np.float64]] = {}
    if simulate and all_projections:
        simulated_event = _busiest_event(all_projections)
        if simulated_event is not None:
            samples = simulate_event(
                all_projections, simulated_event, simulations=simulations, seed=seed
            )

    response = schemas.ProjectionResponse(
        horizon=horizon,
        events=events,
        decay=decay,
        simulated_event=simulated_event,
        simulations=simulations if samples else None,
        players=[_projection_out(p, samples) for p in all_projections[:limit]],
    )
    cache_set(cache_key, response.model_dump_json(), settings.optimize_cache_ttl_seconds)
    return response


@router.post("/plan", response_model=schemas.PlanResponse)
def plan(
    request: schemas.PlanRequest, session: Session = Depends(get_session)
) -> schemas.PlanResponse:
    """Plan squad, transfers, captaincy and chips across the whole horizon.

    Only the first gameweek's moves are meant to be executed. The rest is the
    plan that justifies them — the reason to bank a transfer this week is
    entirely about what you do with it next week, so the later gameweeks have
    to be in the model even though they will be re-solved before they arrive.
    """
    cache_key = _key("plan", request.model_dump_json())
    cached = _cached(cache_key, schemas.PlanResponse)
    if cached is not None:
        return cached

    owned: set[int] | None = None
    team_name: str | None = None
    budget = round(request.budget * 10)
    if request.fpl_team_id is not None:
        with FPLClient() as client:
            try:
                current = fetch_current_squad(client, session, request.fpl_team_id)
            except TeamNotFoundError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        owned = {player.player_id for player in current.squad}
        budget = sum(player.now_cost for player in current.squad) + current.bank
        team_name = current.team_name

    candidates, events = build_horizon_candidates_from_db(
        session, horizon=request.horizon, decay=request.decay, always_include=owned
    )
    result = plan_horizon(
        candidates,
        events,
        budget=budget,
        current_squad_ids=owned,
        free_transfers=request.free_transfers,
        constraints=SquadConstraints(budget=budget, max_per_club=request.max_per_club),
        decay=request.decay,
        chips=frozenset(request.chips),
        solver_time_limit=settings.plan_solver_time_limit_seconds,
    )

    response = schemas.PlanResponse(
        events=events,
        total_expected_points=result.total_expected_points,
        total_hit_cost=result.total_hit_cost,
        solver_status=result.solver_status,
        team_name=team_name,
        gameweeks=[_gameweek_out(gameweek) for gameweek in result.gameweeks],
    )
    cache_set(cache_key, response.model_dump_json(), settings.optimize_cache_ttl_seconds)
    return response


def _key(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()
    return f"fplquant:{prefix}:{digest}"


def _cached[T](cache_key: str, model: type[T]) -> T | None:
    """Read a cached response, treating an undeserializable entry as a miss.

    Same contract as `fplquant.api.routers.optimizer`: a cache is never allowed
    to be a hard dependency for correctness, whether it's unreachable or just
    stale in a way that no longer matches the response schema.
    """
    raw = cache_get(cache_key)
    if raw is None:
        return None
    try:
        return model.model_validate_json(raw)  # type: ignore[attr-defined,no-any-return]
    except ValidationError:
        logger.warning("Discarding stale cache entry for key=%s", cache_key)
        return None


def _busiest_event(projections: list[HorizonProjection]) -> int | None:
    """The first gameweek in the horizon where most of the league plays.

    A round that is already part-played has most clubs sitting out, and
    simulating it would report a floor and ceiling of zero for them — true, but
    not the question anyone is asking.
    """
    counts: dict[int, int] = {}
    for projection in projections:
        for event in projection.events:
            counts.setdefault(event.event, 0)
            if event.fixtures:
                counts[event.event] += 1
    if not counts or max(counts.values()) == 0:
        return None
    busiest = max(counts.values())
    return next(event for event in sorted(counts) if counts[event] >= busiest / 2)


def _projection_out(
    projection: HorizonProjection, samples: Mapping[int, npt.NDArray[np.float64]]
) -> schemas.HorizonProjectionOut:
    outcome = None
    if projection.player_id in samples:
        summary = summarize_player(
            projection.player_id, projection.web_name, samples[projection.player_id]
        )
        outcome = schemas.PlayerOutcomeOut.model_validate(summary)

    return schemas.HorizonProjectionOut(
        player_id=projection.player_id,
        web_name=projection.web_name,
        team_id=projection.team_id,
        team_short_name=projection.team_short_name,
        element_type=projection.element_type,
        now_cost=projection.now_cost,
        total_points=projection.total_points,
        discounted_points=projection.discounted_points,
        next_event_points=projection.next_event_points,
        p_start=projection.usage.p_start,
        expected_minutes=projection.usage.expected_minutes,
        goal_share=projection.usage.goal_share,
        assist_share=projection.usage.assist_share,
        rate_credibility=projection.usage.rate_credibility,
        events=[
            schemas.EventProjectionOut(
                event=event.event,
                points=event.points,
                is_blank=event.is_blank,
                is_double=event.is_double,
                fixtures=[
                    schemas.FixtureProjectionOut.model_validate(fixture)
                    for fixture in event.fixtures
                ],
            )
            for event in projection.events
        ],
        outcome=outcome,
    )


def _gameweek_out(gameweek: GameweekPlan) -> schemas.GameweekPlanOut:
    xi = gameweek.starting_xi
    moves = [
        schemas.TransferMoveOut(
            out=schemas.SquadPlayerOut.model_validate(out_player),
            **{"in": schemas.SquadPlayerOut.model_validate(in_player)},
        )
        for out_player, in_player in zip(
            gameweek.transfers_out, gameweek.transfers_in, strict=False
        )
    ]
    return schemas.GameweekPlanOut(
        event=gameweek.event,
        expected_points=gameweek.expected_points,
        free_transfers_available=gameweek.free_transfers_available,
        hits_taken=gameweek.hits_taken,
        hit_cost=gameweek.hit_cost,
        chip=gameweek.chip,
        transfers=moves,
        squad=[schemas.SquadPlayerOut.model_validate(p) for p in gameweek.squad.players],
        starting_xi=schemas.StartingXIOut(
            formation=xi.formation,
            starters=[schemas.SquadPlayerOut.model_validate(p) for p in xi.starters],
            bench=[schemas.SquadPlayerOut.model_validate(p) for p in xi.bench],
            captain=schemas.SquadPlayerOut.model_validate(xi.captain),
            vice_captain=schemas.SquadPlayerOut.model_validate(xi.vice_captain),
            starting_predicted_points=xi.starting_predicted_points,
            bench_boost_value=xi.bench_boost_value,
            triple_captain_value=xi.triple_captain_value,
        ),
    )
