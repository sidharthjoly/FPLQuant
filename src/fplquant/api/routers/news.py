from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, selectinload

from fplquant.api import schemas
from fplquant.api.deps import get_session
from fplquant.engine.horizon import DEFAULT_HORIZON
from fplquant.models.orm import Player
from fplquant.news.availability import availability_timeline
from fplquant.news.items import NewsCategory
from fplquant.schedule import upcoming_events

router = APIRouter(tags=["news"])


@router.get("/news", response_model=list[schemas.PlayerNewsOut])
def get_news(
    horizon: int = DEFAULT_HORIZON,
    only_time_varying: bool = False,
    session: Session = Depends(get_session),
) -> list[schemas.PlayerNewsOut]:
    """Every player FPL has published news about, and what it means per gameweek.

    `only_time_varying=true` narrows this to the players the news says something
    *datable* about — a ban that expires inside the horizon, a stated return, a
    knock that will have cleared. That is the subset worth acting on, and it is
    usually a few dozen rather than a few hundred: an injury with no return date
    is real news and still carries no time information, so its availability is
    flat and the model has nothing to add to FPL's own number.

    Ordered by when the player comes back, soonest first, with the ones not
    returning inside the horizon last — which is the order a manager deciding
    whether to hold or sell actually wants to read.
    """
    events = upcoming_events(session, horizon)
    if not events:
        return []

    timeline = availability_timeline(session, events)
    players = {
        p.id: p
        for p in session.query(Player).options(selectinload(Player.team)).all()
        if p.id in timeline
    }

    rows = []
    for player_id, entry in timeline.items():
        player = players.get(player_id)
        if player is None or entry.news.category is NewsCategory.AVAILABLE:
            continue
        if only_time_varying and not entry.is_time_varying:
            continue
        rows.append(
            schemas.PlayerNewsOut(
                player_id=player_id,
                web_name=player.web_name,
                team_short_name=player.team.short_name,
                element_type=player.element_type,
                now_cost=player.now_cost,
                chance_of_playing=entry.news.next_round_availability,
                news=schemas.NewsOut.from_availability(entry, events),
            )
        )
    # A player with no return inside the horizon sorts last rather than first,
    # which is what `None` would otherwise do.
    rows.sort(key=lambda r: (r.news.return_event is None, r.news.return_event or 0, r.web_name))
    return rows
