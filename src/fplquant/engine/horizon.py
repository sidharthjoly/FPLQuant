"""Expected points over the next several gameweeks, not just the next one.

Every estimate elsewhere in this codebase answers "how many points will this
player score in their next match". That is the wrong question for almost every
decision an FPL manager actually makes. A transfer is a commitment for weeks,
not for one round; a player with a superb next fixture and four brutal ones
after it is a trap; and the two things that swing a season hardest — double
gameweeks, where a club plays twice in one round, and blanks, where they play
not at all — are invisible to a model that only ever looks at the next match.

So this module projects each player over a horizon of gameweeks, fixture by
fixture. A double gameweek is simply two fixtures summed into one event, which
falls out of the structure rather than needing a special case; a blank is an
event with no fixtures and therefore no points, which is exactly the signal a
manager needs and precisely what a next-fixture model cannot express.

Future gameweeks are discounted. Not because points later are worth less than
points now — they are worth exactly the same — but because a projection five
weeks out is less reliable than one for Saturday, and because you will get to
revise the squad before it arrives. The discount is what stops the optimizer
paying today for a fixture swing it can still buy into in a month's time.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from fplquant.engine.rates import FixtureRates, compute_fixture_rates, compute_team_ratings
from fplquant.engine.scoring import PointsBreakdown, expected_points
from fplquant.engine.usage import PlayerUsage, compute_player_usage, fixture_inputs
from fplquant.models.orm import Player, Team
from fplquant.schedule import get_upcoming_fixtures_by_team_event, upcoming_events

# How many gameweeks ahead to project by default. Five is about the point where
# an FPL projection stops carrying real information: squads change, players get
# injured, and FPL itself only publishes fixture difficulty a few weeks out.
DEFAULT_HORIZON = 5

# Per-gameweek discount. At 0.9 a fixture five weeks away counts for about two
# thirds of one this week, which is roughly how much less certain it is.
DEFAULT_DECAY = 0.9


@dataclass(frozen=True)
class FixtureProjection:
    """One player, one fixture: the goal rates behind it and the points out."""

    fixture_id: int
    event: int
    opponent_team_id: int
    opponent_short_name: str | None
    is_home: bool
    lambda_for: float  # their side's expected goals in this fixture
    lambda_against: float
    breakdown: PointsBreakdown

    @property
    def points(self) -> float:
        return self.breakdown.total


@dataclass(frozen=True)
class EventProjection:
    """One player, one gameweek — which may hold zero, one, or two fixtures."""

    event: int
    fixtures: list[FixtureProjection]
    points: float

    @property
    def is_blank(self) -> bool:
        return not self.fixtures

    @property
    def is_double(self) -> bool:
        return len(self.fixtures) > 1


@dataclass(frozen=True)
class HorizonProjection:
    player_id: int
    web_name: str
    team_id: int
    team_short_name: str
    element_type: int
    now_cost: int
    usage: PlayerUsage
    events: list[EventProjection]
    total_points: float  # undiscounted sum over the horizon
    discounted_points: float  # what the optimizer maximizes
    next_event_points: float  # the first gameweek of the horizon on its own

    @property
    def points_by_event(self) -> dict[int, float]:
        return {event.event: event.points for event in self.events}


def project_horizon(
    session: Session,
    horizon: int = DEFAULT_HORIZON,
    decay: float = DEFAULT_DECAY,
) -> list[HorizonProjection]:
    """Project every player over the next `horizon` gameweeks.

    The heavy work — fitting team ratings and computing usage shares — is done
    once for the whole pool rather than per player, so the cost of a longer
    horizon is only the extra fixtures.
    """
    if not 0 < decay <= 1:
        raise ValueError("decay must be in (0, 1]")

    ratings = compute_team_ratings(session)
    rates_by_fixture = compute_fixture_rates(session, ratings)
    usage_by_player = compute_player_usage(session)
    fixtures_by_team_event = get_upcoming_fixtures_by_team_event(session)
    events = upcoming_events(session, horizon)
    teams_by_id = {team.id: team for team in session.query(Team).all()}

    projections = []
    for player in session.query(Player).all():
        usage = usage_by_player.get(player.id)
        if usage is None:
            continue
        team_fixtures = fixtures_by_team_event.get(player.team_id, {})

        event_projections = []
        for event in events:
            fixture_projections = []
            for fixture in team_fixtures.get(event, []):
                rates = rates_by_fixture.get(fixture.id)
                if rates is None:
                    continue
                is_home = fixture.team_h_id == player.team_id
                fixture_projections.append(
                    _project_fixture(usage, rates, event, is_home, teams_by_id)
                )
            event_projections.append(
                EventProjection(
                    event=event,
                    fixtures=fixture_projections,
                    points=sum(f.points for f in fixture_projections),
                )
            )

        total = sum(e.points for e in event_projections)
        discounted = sum(e.points * decay**index for index, e in enumerate(event_projections))
        team = teams_by_id.get(player.team_id)
        projections.append(
            HorizonProjection(
                player_id=player.id,
                web_name=player.web_name,
                team_id=player.team_id,
                team_short_name=team.short_name if team else "?",
                element_type=player.element_type,
                now_cost=player.now_cost,
                usage=usage,
                events=event_projections,
                total_points=total,
                discounted_points=discounted,
                next_event_points=event_projections[0].points if event_projections else 0.0,
            )
        )
    return sorted(projections, key=lambda p: p.discounted_points, reverse=True)


def _project_fixture(
    usage: PlayerUsage,
    rates: FixtureRates,
    event: int,
    is_home: bool,
    teams_by_id: dict[int, Team],
) -> FixtureProjection:
    lambda_for = rates.lambda_home if is_home else rates.lambda_away
    lambda_against = rates.lambda_away if is_home else rates.lambda_home
    opponent_id = rates.away_team_id if is_home else rates.home_team_id
    opponent = teams_by_id.get(opponent_id)
    return FixtureProjection(
        fixture_id=rates.fixture_id,
        event=event,
        opponent_team_id=opponent_id,
        opponent_short_name=opponent.short_name if opponent else None,
        is_home=is_home,
        lambda_for=lambda_for,
        lambda_against=lambda_against,
        breakdown=expected_points(fixture_inputs(usage, lambda_for, lambda_against)),
    )
