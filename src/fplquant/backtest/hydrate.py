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
    """One fixture per (fixture id) seen, marked played if it is behind us.

    A played fixture has to carry its scoreline, not just the `finished` flag.
    `fplquant.engine.rates.played_fixtures` gates on both, so a fixture list
    with the flag and no scores is an *empty* match record as far as the goal
    model is concerned — every club's credibility stays at zero, the fitted
    correction collapses to 1.0, and the replay silently measures nothing but
    the prior. That is not a hypothetical: it is what this function did until
    the archive started carrying `team_h_score`.
    """
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
        played = row.round < up_to_round
        session.add(
            Fixture(
                fpl_id=fixture_id,
                event=row.round,
                team_h_id=home.id,
                team_a_id=away.id,
                kickoff_time=row.kickoff_time or dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
                finished=played,
                # Only for matches behind the deadline: the scoreline of the
                # round being predicted is exactly the future this replay
                # exists to keep out.
                team_h_score=row.team_h_score if played else None,
                team_a_score=row.team_a_score if played else None,
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

    One row per player-fixture, which is both what the archive holds and what
    the live schema now stores — so a double gameweek arrives here as two rows
    and stays two rows. Attribution matters as much as the totals: the team xG
    that `fplquant.engine.rates` fits against is keyed on the fixture, so
    collapsing a double into one row would hand both matches' xG to one of
    them and none to the other.
    """
    for row in rows:
        if row.round >= up_to_round or row.element not in players:
            continue
        session.add(
            PlayerGameweekStat(
                player_id=players[row.element].id,
                round=row.round,
                fixture_fpl_id=row.fixture,
                opponent_team_fpl_id=row.opponent_team,
                was_home=row.was_home,
                kickoff_time=row.kickoff_time,
                value=row.value,
                selected=row.selected,
                minutes=row.minutes,
                starts=row.starts,
                total_points=row.total_points,
                goals_scored=row.goals_scored,
                assists=row.assists,
                clean_sheets=row.clean_sheets,
                goals_conceded=row.goals_conceded,
                bonus=row.bonus,
                bps=row.bps,
                ict_index=row.ict_index,
                expected_goals=row.expected_goals or 0.0,
                expected_assists=row.expected_assists or 0.0,
                defensive_contribution=row.defensive_contribution,
                clearances_blocks_interceptions=row.clearances_blocks_interceptions,
                recoveries=row.recoveries,
                tackles=row.tackles,
            )
        )
