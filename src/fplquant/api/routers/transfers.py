import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from fplquant.api import schemas
from fplquant.api.cache import cache_get, cache_set
from fplquant.api.deps import get_session
from fplquant.config import settings
from fplquant.data.fpl_client import FPLClient
from fplquant.optimizer.candidates import (
    build_candidates_from_db,
    build_horizon_candidates_from_db,
    build_risk_adjusted_candidates_from_db,
)
from fplquant.optimizer.types import SquadConstraints
from fplquant.transfers.planner import propose_transfers
from fplquant.transfers.search import MoveLine, search_transfer_lines
from fplquant.transfers.team_lookup import (
    CurrentTeam,
    TeamNotFoundError,
    fetch_current_squad,
)

router = APIRouter(prefix="/transfers", tags=["transfers"])
logger = logging.getLogger(__name__)


@router.post("/plan", response_model=schemas.TransferPlanResponse)
def plan_transfers(
    request: schemas.TransferPlanRequest, session: Session = Depends(get_session)
) -> schemas.TransferPlanResponse:
    """Pull a manager's current squad from their public FPL team ID and
    recommend the transfers (if any) worth making this gameweek.
    """
    with FPLClient() as client:
        try:
            current_team = fetch_current_squad(
                client, session, request.fpl_team_id, projection=request.projection
            )
        except TeamNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.chip == "none":
        return _search_from_position(session, request, current_team)

    # A wildcard or free hit is not a choice between moves — it rebuilds the
    # squad outright, with no transfer limit and no hit — so there is nothing
    # to rank and the single-gameweek rebuild is the right answer.
    return _single_gameweek_plan(session, request, current_team)


def _single_gameweek_plan(
    session: Session,
    request: schemas.TransferPlanRequest,
    current_team: CurrentTeam,
) -> schemas.TransferPlanResponse:
    """The original one-week answer: best swap for the next match.

    Still the right tool for a chip week, and the fallback when there is no
    horizon to search — out of season, or before a fixture list has been
    ingested. It cannot value banking a transfer, which is why it is not the
    default any more.
    """
    if request.risk_adjusted:
        candidates = build_risk_adjusted_candidates_from_db(
            session,
            risk_aversion=request.risk_aversion,
            injury_weight=request.injury_weight,
            projection=request.projection,
        )
    else:
        candidates = build_candidates_from_db(session, projection=request.projection)

    plan = propose_transfers(
        current_team.squad,
        candidates,
        bank=current_team.bank,
        free_transfers=request.free_transfers,
        max_per_club=request.max_per_club,
        chip=request.chip,
    )

    return schemas.TransferPlanResponse(
        team_name=current_team.team_name,
        event_id=current_team.event_id,
        bank=current_team.bank,
        chip=plan.chip,
        current_squad=[schemas.SquadPlayerOut.model_validate(p) for p in current_team.squad],
        transfers=[
            schemas.TransferPairOut(
                out=schemas.SquadPlayerOut.model_validate(pair.out),
                player_in=schemas.SquadPlayerOut.model_validate(pair.in_),
            )
            for pair in plan.transfers
        ],
        transfers_made=plan.transfers_made,
        free_transfers=plan.free_transfers,
        hit_cost=plan.hit_cost,
        points_gain_before_hit=plan.points_gain_before_hit,
        points_gain_after_hit=plan.points_gain_after_hit,
        worth_it=plan.worth_it,
        resulting_squad=[
            schemas.SquadPlayerOut.model_validate(p) for p in plan.resulting_squad.players
        ],
        starting_xi=schemas.StartingXIOut(
            formation=plan.starting_xi.formation,
            starters=[schemas.SquadPlayerOut.model_validate(p) for p in plan.starting_xi.starters],
            bench=[schemas.SquadPlayerOut.model_validate(p) for p in plan.starting_xi.bench],
            captain=schemas.SquadPlayerOut.model_validate(plan.starting_xi.captain),
            vice_captain=schemas.SquadPlayerOut.model_validate(plan.starting_xi.vice_captain),
            starting_predicted_points=plan.starting_xi.starting_predicted_points,
            bench_boost_value=plan.starting_xi.bench_boost_value,
            triple_captain_value=plan.starting_xi.triple_captain_value,
        ),
    )


def _cache_key(request: schemas.TransferPlanRequest) -> str:
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    return f"fplquant:transfers:v1:{digest}"


def _line_out(line: MoveLine) -> schemas.TransferLineOut:
    return schemas.TransferLineOut(
        transfers=[
            schemas.TransferPairOut(
                out=schemas.SquadPlayerOut.model_validate(out),
                player_in=schemas.SquadPlayerOut.model_validate(incoming),
            )
            for out, incoming in zip(line.transfers_out, line.transfers_in, strict=False)
        ],
        hit_cost=line.hit_cost,
        gain_vs_hold=line.gain_vs_hold,
        horizon_points=line.horizon_points,
        is_hold=line.is_hold,
    )


def _search_from_position(
    session: Session,
    request: schemas.TransferPlanRequest,
    current_team: CurrentTeam,
) -> schemas.TransferPlanResponse:
    """Search the horizon from the squad the manager actually owns.

    The moves come back ranked with the value of holding among them, rather
    than as a single verdict on a single swap — see
    `fplquant.transfers.search` for why one gameweek cannot answer this.

    Cached, because each line is its own integer program over the whole
    horizon and a cold request solves several.
    """
    cache_key = _cache_key(request)
    cached = cache_get(cache_key)
    if cached is not None:
        try:
            return schemas.TransferPlanResponse.model_validate_json(cached)
        except ValidationError:
            logger.warning("Discarding stale cache entry for key=%s", cache_key)

    owned = {player.player_id for player in current_team.squad}
    budget = sum(player.now_cost for player in current_team.squad) + current_team.bank
    candidates, events = build_horizon_candidates_from_db(
        session, horizon=request.horizon, always_include=owned
    )
    if not events:
        # No upcoming fixture to search over — preseason, out of season, or a
        # database part-way through its first ingest. A horizon of nothing is
        # not a reason to refuse the question the old path can still answer.
        logger.warning("No upcoming gameweeks to search; answering for the next match only.")
        return _single_gameweek_plan(session, request, current_team)

    search = search_transfer_lines(
        candidates,
        events,
        budget=budget,
        current_squad_ids=owned,
        free_transfers=request.free_transfers,
        constraints=SquadConstraints(budget=budget, max_per_club=request.max_per_club),
        lines=request.lines,
        solver_time_limit=settings.plan_solver_time_limit_seconds,
    )
    best = search.best
    first = best.plan.gameweeks[0]

    response = schemas.TransferPlanResponse(
        team_name=current_team.team_name,
        event_id=current_team.event_id,
        bank=current_team.bank,
        chip="none",
        current_squad=[schemas.SquadPlayerOut.model_validate(p) for p in current_team.squad],
        transfers=_line_out(best).transfers,
        transfers_made=len(best.transfers_in),
        free_transfers=request.free_transfers,
        hit_cost=best.hit_cost,
        # Both gains are now horizon quantities: what this line is worth
        # against banking the transfer, before and after the hit it pays.
        points_gain_before_hit=best.gain_vs_hold + best.hit_cost,
        points_gain_after_hit=best.gain_vs_hold,
        worth_it=not best.is_hold and best.worth_it,
        resulting_squad=[schemas.SquadPlayerOut.model_validate(p) for p in first.squad.players],
        starting_xi=schemas.StartingXIOut(
            formation=first.starting_xi.formation,
            starters=[schemas.SquadPlayerOut.model_validate(p) for p in first.starting_xi.starters],
            bench=[schemas.SquadPlayerOut.model_validate(p) for p in first.starting_xi.bench],
            captain=schemas.SquadPlayerOut.model_validate(first.starting_xi.captain),
            vice_captain=schemas.SquadPlayerOut.model_validate(first.starting_xi.vice_captain),
            starting_predicted_points=first.starting_xi.starting_predicted_points,
            bench_boost_value=first.starting_xi.bench_boost_value,
            triple_captain_value=first.starting_xi.triple_captain_value,
        ),
        horizon_events=events,
        lines=[_line_out(line) for line in search.lines],
        lines_searched=search.searched,
        lines_truncated=search.truncated,
    )
    cache_set(cache_key, response.model_dump_json(), settings.optimize_cache_ttl_seconds)
    return response
