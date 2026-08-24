import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fplquant.api import schemas
from fplquant.api.deps import get_session
from fplquant.data.fpl_client import FPLClient
from fplquant.schedule import upcoming_events

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/next-deadline", response_model=schemas.NextDeadlineOut)
def next_deadline() -> schemas.NextDeadlineOut:
    """The next gameweek's transfer deadline, straight from FPL's own
    bootstrap-static events — used for the header countdown clock.

    Filters on the deadline itself rather than the `finished` flag: FPL
    doesn't flip `finished` until a gameweek's matches are fully played out
    and bonus points are confirmed, which can lag the actual deadline by a
    day or more. During that lag the old "first unfinished event" pick
    would still be the gameweek whose deadline had already passed.
    """
    with FPLClient() as client:
        bootstrap = client.get_bootstrap_static()

    now = dt.datetime.now(dt.UTC)
    upcoming = []
    for event in bootstrap["events"]:
        deadline_time = event.get("deadline_time")
        if not deadline_time:
            continue
        deadline_dt = dt.datetime.fromisoformat(deadline_time.replace("Z", "+00:00"))
        if deadline_dt > now:
            upcoming.append(event)
    if not upcoming:
        return schemas.NextDeadlineOut(deadline=None, gameweek=None)

    next_event = min(upcoming, key=lambda event: event["deadline_time"])
    return schemas.NextDeadlineOut(deadline=next_event["deadline_time"], gameweek=next_event["id"])


# A full season, so the query is "everything still to come" rather than a
# window — the caller wants the ceiling, not a page of results.
_WHOLE_SEASON = 38


@router.get("/remaining-gameweeks", response_model=schemas.RemainingGameweeksOut)
def remaining_gameweeks(
    session: Session = Depends(get_session),
) -> schemas.RemainingGameweeksOut:
    """How many gameweeks are left to plan over.

    The planner clamps its horizon to what the fixture list can actually
    support, which is the right behaviour and an invisible one: late in a
    season, asking for eight gameweeks quietly returns three. This lets the
    dashboard offer only the horizons that still mean something, so the
    control reflects the season rather than a number picked at build time.
    """
    events = upcoming_events(session, horizon=_WHOLE_SEASON)
    return schemas.RemainingGameweeksOut(count=len(events), events=events)
