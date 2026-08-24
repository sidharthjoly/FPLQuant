from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload

from fplquant.api import schemas
from fplquant.api.deps import get_session
from fplquant.form.fixtures import compute_fixture_adjusted_scores
from fplquant.form.scoring import compute_form_scores
from fplquant.lineup.starts import (
    StartProbability,
    combined_start_probability,
    compute_start_probabilities,
)
from fplquant.models.orm import Player, PlayerGameweekStat
from fplquant.risk.injury import compute_injury_risk_scores
from fplquant.similarity.finder import find_cheaper_alternatives, find_similar_players
from fplquant.similarity.vectors import build_player_vectors
from fplquant.utils import normalize_text

router = APIRouter(prefix="/players", tags=["players"])

# Relevance tiers for player search — lower sorts first. Checked against
# web_name, first_name, second_name, and the full "first second" name, so a
# search matches on either first or last name, not just FPL's display name.
_EXACT = 0
_PREFIX = 1
_SUBSTRING = 2


def _search_relevance(player: Player, query: str) -> int | None:
    """Relevance tier for `player` against `query`, or None if it doesn't
    match at all. web_name is checked first since that's what's actually
    shown in results and most likely to be what the user typed."""
    q = normalize_text(query)
    web = normalize_text(player.web_name)
    first = normalize_text(player.first_name)
    second = normalize_text(player.second_name)
    full = f"{first} {second}"

    if q in (web, second, full):
        return _EXACT
    name_parts = (web, second, *full.split())
    if any(part.startswith(q) for part in name_parts):
        return _PREFIX
    if q in web or q in first or q in second:
        return _SUBSTRING
    return None


def _photo_url(code: int | None) -> str | None:
    # The official FPL asset CDN — the same photo URL pattern the live FPL
    # site itself uses, keyed on the bootstrap-static "code" field.
    if code is None:
        return None
    return f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png"


def _to_player_out(player: Player) -> schemas.PlayerOut:
    return schemas.PlayerOut(
        id=player.id,
        fpl_id=player.fpl_id,
        web_name=player.web_name,
        full_name=f"{player.first_name} {player.second_name}",
        team_id=player.team_id,
        team_short_name=player.team.short_name,
        element_type=player.element_type,
        now_cost=player.now_cost,
        status=player.status,
        selected_by_percent=player.selected_by_percent,
        form=player.form,
        ep_next=player.ep_next,
        nationality=player.nationality,
        photo_url=_photo_url(player.code),
    )


def _to_start_odds(start: StartProbability, availability: float) -> schemas.StartOddsOut:
    return schemas.StartOddsOut(
        player_id=start.player_id,
        appearances=start.appearances,
        start_probability=combined_start_probability(start, availability),
        availability=availability,
        baseline_probability=start.baseline_probability,
        adjusted_probability=start.adjusted_probability,
        evidence_weight=start.evidence_weight,
        fatigue_index=start.fatigue_index,
        minutes_load=start.minutes_load,
        rest_days=start.rest_days,
        formation_factor=start.formation_factor,
        team_shape=start.team_shape,
        recent_team_shape=start.recent_team_shape,
    )


@router.get("", response_model=list[schemas.PlayerOut])
def list_players(
    position: int | None = None,
    team_id: int | None = None,
    max_cost: int | None = None,
    search: str | None = None,
    sort: Literal["popularity"] | None = None,
    limit: int | None = None,
    session: Session = Depends(get_session),
) -> list[schemas.PlayerOut]:
    query = session.query(Player).options(selectinload(Player.team))
    if position is not None:
        query = query.filter(Player.element_type == position)
    if team_id is not None:
        query = query.filter(Player.team_id == team_id)
    if max_cost is not None:
        query = query.filter(Player.now_cost <= max_cost)

    players = query.all()
    if search:
        # An active search query wins over `sort` — relevance is what
        # matters once the user has typed something, same as Google.
        scored = [(p, _search_relevance(p, search)) for p in players]
        matches = [(p, score) for p, score in scored if score is not None]
        matches.sort(key=lambda pair: (pair[1], pair[0].web_name))
        players = [p for p, _ in matches]
    elif sort == "popularity":
        players.sort(key=lambda p: p.selected_by_percent, reverse=True)

    if limit is not None:
        players = players[:limit]

    return [_to_player_out(p) for p in players]


@router.get("/{player_id}", response_model=schemas.PlayerDetailOut)
def get_player(player_id: int, session: Session = Depends(get_session)) -> schemas.PlayerDetailOut:
    player = session.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    form = next((s for s in compute_form_scores(session) if s.player_id == player_id), None)
    risk = next((s for s in compute_injury_risk_scores(session) if s.player_id == player_id), None)

    # The start probabilities are computed here rather than left to
    # `compute_fixture_adjusted_scores` to compute internally, so the full
    # breakdown is available for `start_odds` without walking every player's
    # gameweek history a second time.
    starts = compute_start_probabilities(session)
    start = next((s for s in starts if s.player_id == player_id), None)
    multipliers = {s.player_id: s.lineup_multiplier for s in starts}
    fixtures = compute_fixture_adjusted_scores(session, lineup_multipliers=multipliers)
    fixture = next((s for s in fixtures if s.player_id == player_id), None)
    availability = fixture.chance_of_playing if fixture else 1.0

    return schemas.PlayerDetailOut(
        **_to_player_out(player).model_dump(),
        form_score=schemas.FormScoreOut.model_validate(form) if form else None,
        injury_risk=schemas.InjuryRiskOut.model_validate(risk) if risk else None,
        next_opponent=fixture.opponent_short_name if fixture else None,
        next_opponent_is_home=fixture.is_home if fixture else None,
        fixture_difficulty=fixture.difficulty if fixture else None,
        chance_of_playing=availability,
        start_odds=_to_start_odds(start, availability) if start else None,
    )


@router.get("/{player_id}/history", response_model=list[schemas.PlayerGameweekStatOut])
def get_player_history(
    player_id: int, session: Session = Depends(get_session)
) -> list[schemas.PlayerGameweekStatOut]:
    """Per-gameweek points/price/minutes for one player, in round order —
    the series a form-trend sparkline is drawn from."""
    player = session.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    stats = (
        session.query(PlayerGameweekStat)
        .filter_by(player_id=player_id)
        .order_by(PlayerGameweekStat.round)
        .all()
    )
    return [schemas.PlayerGameweekStatOut.model_validate(s) for s in stats]


@router.get("/{player_id}/similar", response_model=list[schemas.SimilarPlayerOut])
def get_similar_players(
    player_id: int,
    top: int = 5,
    cheaper_only: bool = False,
    any_position: bool = False,
    session: Session = Depends(get_session),
) -> list[schemas.SimilarPlayerOut]:
    player = session.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    vectors = build_player_vectors(session)
    if cheaper_only:
        results = find_cheaper_alternatives(vectors, player_id, k=top)
    else:
        results = find_similar_players(
            vectors, player_id, k=top, same_position_only=not any_position
        )

    teams_by_player_id = {
        p.id: p.team.short_name
        for p in session.query(Player)
        .options(selectinload(Player.team))
        .filter(Player.id.in_([r.player_id for r in results]))
        .all()
    }
    return [
        schemas.SimilarPlayerOut(
            player_id=r.player_id,
            web_name=r.web_name,
            team_id=r.team_id,
            team_short_name=teams_by_player_id.get(r.player_id, ""),
            now_cost=r.now_cost,
            similarity=r.similarity,
        )
        for r in results
    ]
