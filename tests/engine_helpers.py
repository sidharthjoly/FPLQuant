"""Shared builders for the engine tests.

Builds a miniature league in the database. Small on purpose: the engine's
behaviour — shares that sum to one, slots that add up to eleven, a fixture's
goals split between two sides — is easier to assert on four clubs than on
twenty, and none of it depends on the league being full size.
"""

import datetime as dt

from sqlalchemy.orm import Session

from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team

GKP, DEF, MID, FWD = 1, 2, 3, 4
SEASON_START = dt.datetime(2026, 8, 15, 14, 0, tzinfo=dt.UTC)

# A squad big enough to fill an XI with a bench, in FPL position terms.
SQUAD_TEMPLATE: list[tuple[int, int]] = (
    [(GKP, 50), (GKP, 40)]
    + [(DEF, 60), (DEF, 55), (DEF, 50), (DEF, 45), (DEF, 40), (DEF, 40)]
    + [(MID, 90), (MID, 70), (MID, 60), (MID, 50), (MID, 45), (MID, 40)]
    + [(FWD, 100), (FWD, 65), (FWD, 45)]
)


def make_team(
    session: Session,
    fpl_id: int,
    short_name: str,
    *,
    strength: int = 3,
    attack: int = 0,
    defence: int = 0,
) -> Team:
    """A club. `attack`/`defence` default to 0, which is what FPL actually
    publishes in preseason and the case the priors have to survive."""
    team = Team(
        fpl_id=fpl_id,
        name=short_name,
        short_name=short_name,
        strength_overall_home=strength,
        strength_overall_away=strength,
        strength_attack_home=attack,
        strength_attack_away=attack,
        strength_defence_home=defence,
        strength_defence_away=defence,
    )
    session.add(team)
    session.flush()
    return team


def make_player(
    session: Session,
    team: Team,
    *,
    fpl_id: int,
    element_type: int = MID,
    now_cost: int = 60,
    status: str = "a",
    web_name: str | None = None,
    chance_of_playing_next_round: int | None = None,
) -> Player:
    player = Player(
        fpl_id=fpl_id,
        team_id=team.id,
        first_name="P",
        second_name=str(fpl_id),
        web_name=web_name or f"P{fpl_id}",
        element_type=element_type,
        now_cost=now_cost,
        status=status,
        ep_next=4.0,
        chance_of_playing_next_round=chance_of_playing_next_round,
    )
    session.add(player)
    session.flush()
    return player


def make_squad(session: Session, team: Team, *, first_fpl_id: int) -> list[Player]:
    """A full FPL-shaped squad for one club, priced so first choices are dearer."""
    return [
        make_player(
            session,
            team,
            fpl_id=first_fpl_id + offset,
            element_type=position,
            now_cost=cost,
        )
        for offset, (position, cost) in enumerate(SQUAD_TEMPLATE)
    ]


def make_fixture(
    session: Session,
    home: Team,
    away: Team,
    *,
    fpl_id: int,
    event: int,
    finished: bool = False,
    home_score: int | None = None,
    away_score: int | None = None,
    kickoff: dt.datetime | None = None,
) -> Fixture:
    fixture = Fixture(
        fpl_id=fpl_id,
        event=event,
        team_h_id=home.id,
        team_a_id=away.id,
        team_h_score=home_score,
        team_a_score=away_score,
        team_h_difficulty=3,
        team_a_difficulty=3,
        kickoff_time=kickoff or SEASON_START + dt.timedelta(days=7 * (event - 1)),
        finished=finished,
    )
    session.add(fixture)
    session.flush()
    return fixture


def make_stat(
    session: Session,
    player: Player,
    *,
    round_number: int,
    minutes: int = 90,
    starts: int | None = 1,
    total_points: int = 4,
    expected_goals: float = 0.0,
    expected_assists: float = 0.0,
    bonus: int = 0,
    fixture: Fixture | None = None,
    was_home: bool | None = None,
) -> PlayerGameweekStat:
    stat = PlayerGameweekStat(
        player_id=player.id,
        round=round_number,
        minutes=minutes,
        starts=starts,
        total_points=total_points,
        expected_goals=expected_goals,
        expected_assists=expected_assists,
        bonus=bonus,
        fixture_fpl_id=fixture.fpl_id if fixture else None,
        was_home=was_home,
        kickoff_time=SEASON_START + dt.timedelta(days=7 * (round_number - 1)),
    )
    session.add(stat)
    session.flush()
    return stat


def make_league(session: Session, teams: int = 4) -> list[Team]:
    """`teams` clubs, each with a full squad, and no matches played yet."""
    built = []
    for index in range(teams):
        team = make_team(session, fpl_id=index + 1, short_name=f"T{index + 1}")
        make_squad(session, team, first_fpl_id=100 * (index + 1))
        built.append(team)
    return built


def make_round(
    session: Session, teams: list[Team], event: int, *, first_fpl_id: int = 1000
) -> None:
    """One fixture per pair of clubs in `teams`, all unplayed."""
    for index in range(0, len(teams) - 1, 2):
        make_fixture(
            session,
            teams[index],
            teams[index + 1],
            fpl_id=first_fpl_id + event * 10 + index,
            event=event,
        )
