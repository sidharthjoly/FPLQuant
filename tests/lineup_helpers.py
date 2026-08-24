"""Shared builders for the lineup tests."""

import datetime as dt

from sqlalchemy.orm import Session

from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team

GKP, DEF, MID, FWD = 1, 2, 3, 4
SEASON_START = dt.datetime(2026, 8, 15, 14, 0, tzinfo=dt.UTC)


def make_team(session: Session, fpl_id: int = 1, short_name: str = "ARS") -> Team:
    team = Team(
        fpl_id=fpl_id,
        name=short_name,
        short_name=short_name,
        strength_attack_home=1000,
        strength_attack_away=1000,
        strength_defence_home=1000,
        strength_defence_away=1000,
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
    status: str = "a",
    ep_next: float = 5.0,
) -> Player:
    player = Player(
        fpl_id=fpl_id,
        team_id=team.id,
        first_name="P",
        second_name=str(fpl_id),
        web_name=f"P{fpl_id}",
        element_type=element_type,
        now_cost=60,
        status=status,
        ep_next=ep_next,
    )
    session.add(player)
    session.flush()
    return player


def make_stat(
    session: Session,
    player: Player,
    *,
    round_number: int,
    minutes: int = 90,
    starts: int | None = 1,
    total_points: int = 4,
    kickoff: dt.datetime | None = None,
) -> PlayerGameweekStat:
    stat = PlayerGameweekStat(
        player_id=player.id,
        round=round_number,
        minutes=minutes,
        starts=starts,
        total_points=total_points,
        kickoff_time=kickoff or SEASON_START + dt.timedelta(days=7 * (round_number - 1)),
    )
    session.add(stat)
    session.flush()
    return stat


def make_next_fixture(
    session: Session, home: Team, away: Team, *, kickoff: dt.datetime, fpl_id: int = 900
) -> Fixture:
    fixture = Fixture(
        fpl_id=fpl_id,
        event=99,
        team_h_id=home.id,
        team_a_id=away.id,
        team_h_difficulty=3,
        team_a_difficulty=3,
        kickoff_time=kickoff,
        finished=False,
    )
    session.add(fixture)
    session.flush()
    return fixture
