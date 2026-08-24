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
