import hashlib
import logging

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session

from fplquant.api import schemas
from fplquant.api.cache import cache_get, cache_set
from fplquant.api.deps import get_session
from fplquant.config import settings
from fplquant.optimizer.candidates import (
    build_candidates_from_db,
    build_risk_adjusted_candidates_from_db,
)
from fplquant.optimizer.squad import optimize_squad
from fplquant.optimizer.starting_xi import select_starting_xi
from fplquant.optimizer.types import SquadConstraints

router = APIRouter(tags=["optimizer"])
logger = logging.getLogger(__name__)


# Bumped when a request that hashes the same starts meaning something else.
# v2: `projection` was added and defaults to the engine, so every v1 entry
# holds a form-path squad for what is now an engine-path request. Without the
# bump the first call after a deploy serves the old answer from cache and the
# change looks like it did nothing.
_CACHE_VERSION = "v2"


def _cache_key(request: schemas.OptimizeRequest) -> str:
    payload = request.model_dump_json()
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"fplquant:optimize:{_CACHE_VERSION}:{digest}"


@router.post("/optimize", response_model=schemas.OptimizeResponse)
def optimize(
    request: schemas.OptimizeRequest, session: Session = Depends(get_session)
) -> schemas.OptimizeResponse:
    """Select an optimal squad. Results are cached in Redis (keyed on the
    request parameters) since the ILP solve — and, for risk_adjusted
    requests, the underlying form/volatility/injury-risk computations — are
    the most expensive operations this API performs.
    """
    cache_key = _cache_key(request)
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return schemas.OptimizeResponse.model_validate_json(cached)
        except ValidationError:
            # A cache entry from before a response-schema change (e.g. a new
            # required field) — treat exactly like a cache miss rather than
            # failing the request. A cache is never allowed to be a hard
            # dependency for correctness, whether it's unreachable (see
            # cache.py) or just stale in a way that no longer deserializes.
            logger.warning("Discarding stale cache entry for key=%s", cache_key)

    constraints = SquadConstraints(
        budget=round(request.budget * 10), max_per_club=request.max_per_club
    )
    if request.risk_adjusted:
        candidates = build_risk_adjusted_candidates_from_db(
            session,
            risk_aversion=request.risk_aversion,
            injury_weight=request.injury_weight,
            projection=request.projection,
        )
    else:
        candidates = build_candidates_from_db(session, projection=request.projection)

    forced_formation: tuple[int, int, int] | None = None
    if request.formation is not None:
        d, m, f = (int(part) for part in request.formation.split("-"))
        forced_formation = (d, m, f)

    squad = optimize_squad(candidates, constraints)
    xi = select_starting_xi(squad.players, forced_formation)
    response = schemas.OptimizeResponse(
        total_cost=squad.total_cost,
        total_predicted_points=squad.total_predicted_points,
        squad=[schemas.SquadPlayerOut.model_validate(p) for p in squad.players],
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

    cache_set(cache_key, response.model_dump_json(), settings.optimize_cache_ttl_seconds)
    return response
