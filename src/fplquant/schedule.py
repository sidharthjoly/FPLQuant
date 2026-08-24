"""Fixture-calendar queries.

Kept separate from the modules that consume it so both the form pipeline and
the lineup pipeline can ask "when does this club play next" without importing
each other.
"""

from sqlalchemy.orm import Session

from fplquant.models.orm import Fixture


def get_next_fixture_by_team(session: Session) -> dict[int, Fixture]:
    """Each team's next unplayed fixture, keyed by team_id.

    Ordered by kickoff time so this is genuinely the *next* match, not just
    any upcoming one. Teams with no unplayed fixture scheduled yet (e.g. a
    blank gameweek before the next round is confirmed) are simply absent.
    """
    fixtures = (
        session.query(Fixture)
        .filter(Fixture.finished.is_(False), Fixture.kickoff_time.isnot(None))
        .order_by(Fixture.kickoff_time.asc())
        .all()
    )
    next_by_team: dict[int, Fixture] = {}
    for fixture in fixtures:
        for team_id in (fixture.team_h_id, fixture.team_a_id):
            next_by_team.setdefault(team_id, fixture)
    return next_by_team


def get_upcoming_fixtures_by_team_event(session: Session) -> dict[int, dict[int, list[Fixture]]]:
    """team_id -> event -> the fixtures that club still has to play in that round.

    A *list* per event rather than a single fixture, because both of the cases
    that make multi-gameweek planning worth doing are cases where the count
    isn't one. A double gameweek gives a club two fixtures in one round and is
    the single biggest swing available to an FPL manager; a blank gives them
    none, and a squad full of blanking clubs is how a good season gets thrown
    away. A "next fixture per team" lookup — which is what
    `get_next_fixture_by_team` deliberately is — cannot represent either.

    Fixtures with no event assigned are excluded: a postponed match that FPL
    has not yet rescheduled has no round to be planned into, and guessing one
    would put points in a gameweek that may never happen.
    """
    fixtures = (
        session.query(Fixture)
        .filter(Fixture.finished.is_(False), Fixture.event.isnot(None))
        .order_by(Fixture.event.asc())
        .all()
    )
    by_team_event: dict[int, dict[int, list[Fixture]]] = {}
    for fixture in fixtures:
        assert fixture.event is not None  # filtered above; narrows the type
        for team_id in (fixture.team_h_id, fixture.team_a_id):
            by_team_event.setdefault(team_id, {}).setdefault(fixture.event, []).append(fixture)
    return by_team_event


def upcoming_events(session: Session, horizon: int) -> list[int]:
    """The next `horizon` gameweek numbers that still have fixtures to play.

    Taken from the fixture list rather than counted forward from the current
    round, so a gameweek that has been entirely wiped out never appears and the
    horizon always covers `horizon` rounds of actual football.
    """
    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    events = (
        session.query(Fixture.event)
        .filter(Fixture.finished.is_(False), Fixture.event.isnot(None))
        .distinct()
        .all()
    )
    return sorted({event for (event,) in events})[:horizon]
