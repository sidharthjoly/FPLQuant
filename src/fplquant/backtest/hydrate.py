"""Rebuild a database as it stood before a past deadline.

The point of a backtest is to run *the engine*, not a second implementation of
it that happens to agree. So rather than reimplementing the model over the
archive's columns, this reconstructs the world the engine expects — teams,
players, fixtures, and gameweek history — restricted to what was knowable
before a given round, and hands it to `project_horizon` unchanged. If the
engine changes, the backtest follows it automatically; a parallel
implementation would quietly stop measuring the thing being shipped.

Everything is built in an in-memory database, so a replay never touches the
real one.

Two honest limits. The archive carries no team strength ratings, so the goal
model's prior falls back to squad value — which is what happens live too, since
FPL publishes those columns as zero for much of a season, but it means the
backtest cannot measure the branch that uses them. And it carries no injury
news: `status` is set to available for everyone, so the hard availability gate
is effectively off. That makes this a test of the *scoring* model rather than of
the whole pipeline, and it flatters nobody in particular — the same assumption
applies to the baselines it is compared against.
"""

import datetime as dt

from sqlalchemy.orm import Session, sessionmaker

from fplquant.models.base import Base, make_engine
from fplquant.models.orm import (
    Fixture,
    HistoricalPlayerGameweek,
    Player,
    PlayerGameweekStat,
    Team,
)

# FPL position labels in the archive, mapped to this schema's element types.
ELEMENT_TYPES = {"GK": 1, "GKP": 1, "DEF": 2, "MID": 3, "AM": 3, "FWD": 4}


def _team_names_by_id(rows: list[HistoricalPlayerGameweek]) -> dict[int, str]:
    """FPL team id -> club name, derived from the rows themselves.

    The archive stores a row's own club as a name and its opponent's as an id,
    so every fixture pairs a name with an id from the other side's perspective.
    Reading both directions recovers the mapping without hardcoding a season's
    club list, which changes every year with promotion and relegation.
    """
    by_fixture: dict[int, list[HistoricalPlayerGameweek]] = {}
    for row in rows:
        by_fixture.setdefault(row.fixture, []).append(row)

    names: dict[int, str] = {}
    for group in by_fixture.values():
        home = next((r for r in group if r.was_home), None)
        away = next((r for r in group if r.was_home is False), None)
        if home is not None and home.opponent_team and away is not None and away.team:
            names[home.opponent_team] = away.team
        if away is not None and away.opponent_team and home is not None and home.team:
            names[away.opponent_team] = home.team
    return names


def hydrate(
    rows: list[HistoricalPlayerGameweek], up_to_round: int
) -> tuple[Session, dict[int, int]]:
    """A session holding the season as of just before `up_to_round`.

    Returns the session and a map from the archive's element id to the created
    player's primary key, so results can be joined back to actual outcomes.

    Rounds before `up_to_round` become played fixtures with gameweek history;
    `up_to_round` itself becomes the upcoming fixture with no history attached.
    Nothing from `up_to_round` or later reaches the player rows — including
    price, which is taken from the most recent *earlier* round, because a price
    is only known once that gameweek's row exists.
    """
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    names_by_id = _team_names_by_id(rows)
    name_to_id = {name: fpl_id for fpl_id, name in names_by_id.items()}

    teams: dict[str, Team] = {}
    for fpl_id, name in sorted(names_by_id.items()):
        team = Team(fpl_id=fpl_id, name=name, short_name=name[:3].upper())
        session.add(team)
        teams[name] = team
    session.flush()

    # Players carry their state as of the last round before the one predicted.
    latest: dict[int, HistoricalPlayerGameweek] = {}
    for row in rows:
        if row.round >= up_to_round:
            continue
        seen = latest.get(row.element)
        if seen is None or row.round > seen.round:
            latest[row.element] = row
    # A player with no earlier round still has to exist to be predicted for, so
    # fall back to their first appearance in the round itself for identity and
    # price. This is the debutant case, and their history stays empty.
    for row in rows:
        if row.round == up_to_round and row.element not in latest:
            latest[row.element] = row

    players: dict[int, Player] = {}
    for element, row in latest.items():
        club = teams.get(row.team or "")
        if club is None:
            continue
        player = Player(
            fpl_id=element,
            team_id=club.id,
            first_name=row.name,
            second_name="",
            web_name=row.name,
            element_type=ELEMENT_TYPES.get(row.position or "MID", 3),
            now_cost=row.value,
            status="a",  # the archive carries no injury news; see the module docstring
            ep_next=0.0,
        )
        session.add(player)
        players[element] = player
    session.flush()

    _add_fixtures(session, rows, teams, name_to_id, up_to_round)
    _add_history(session, rows, players, up_to_round)
    session.flush()
    return session, {element: player.id for element, player in players.items()}


def _add_fixtures(
    session: Session,
    rows: list[HistoricalPlayerGameweek],
    teams: dict[str, Team],
    name_to_id: dict[str, int],
    up_to_round: int,
) -> None:
    """One fixture per (fixture id) seen, marked played if it is behind us."""
    seen: dict[int, HistoricalPlayerGameweek] = {}
    for row in rows:
        if row.round > up_to_round or row.was_home is None:
            continue
        if row.fixture not in seen and row.was_home:
            seen[row.fixture] = row

    for fixture_id, row in seen.items():
        home = teams.get(row.team or "")
        away_name = next((n for n, i in name_to_id.items() if i == row.opponent_team), None)
        away = teams.get(away_name or "")
        if home is None or away is None:
            continue
        session.add(
            Fixture(
                fpl_id=fixture_id,
                event=row.round,
                team_h_id=home.id,
                team_a_id=away.id,
                kickoff_time=row.kickoff_time or dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
                finished=row.round < up_to_round,
                team_h_difficulty=3,
                team_a_difficulty=3,
            )
        )


def _add_history(
    session: Session,
    rows: list[HistoricalPlayerGameweek],
    players: dict[int, Player],
    up_to_round: int,
) -> None:
    """Gameweek stats for rounds strictly before the one being predicted.

    A double gameweek gives one player two archive rows in a round, but
    `PlayerGameweekStat` is unique on (player, round) — the same limitation the
    live schema has. The rows are summed rather than one being dropped, so a
    player's minutes and goals for that round are still right.
    """
    merged: dict[tuple[int, int], PlayerGameweekStat] = {}
    for row in rows:
        if row.round >= up_to_round or row.element not in players:
            continue
        key = (row.element, row.round)
        stat = merged.get(key)
        if stat is None:
            stat = PlayerGameweekStat(
                player_id=players[row.element].id,
                round=row.round,
                fixture_fpl_id=row.fixture,
                was_home=row.was_home,
                kickoff_time=row.kickoff_time,
                value=row.value,
                selected=row.selected,
                # Explicit zeros: column defaults are applied by the database
                # on insert, so these are still None in Python until a flush,
                # and this accumulates into them before one happens.
                minutes=0,
                starts=0,
                total_points=0,
                goals_scored=0,
                assists=0,
                bonus=0,
                bps=0,
                ict_index=0.0,
                expected_goals=0.0,
                expected_assists=0.0,
            )
            merged[key] = stat
            session.add(stat)
        stat.minutes += row.minutes
        stat.starts = (stat.starts or 0) + (row.starts or 0)
        stat.total_points += row.total_points
        stat.goals_scored += row.goals_scored
        stat.assists += row.assists
        stat.bonus += row.bonus
        stat.bps += row.bps
        stat.ict_index += row.ict_index
        stat.expected_goals += row.expected_goals or 0.0
        stat.expected_assists += row.expected_assists or 0.0
