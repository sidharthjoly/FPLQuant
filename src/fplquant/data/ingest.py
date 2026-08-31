import datetime as dt
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from fplquant.data.fpl_client import FPLClient
from fplquant.models.base import session_scope
from fplquant.models.orm import (
    Fixture,
    Player,
    PlayerGameweekStat,
    PlayerSnapshot,
    Team,
    TeamSnapshot,
)
from fplquant.utils import as_float

logger = logging.getLogger(__name__)


def _parse_kickoff(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_birth_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value)


def _price_change_likelihood(raw: dict[str, Any]) -> int | None:
    """How many price changes FPL projects for this player, signed.

    `price_change_projections` is a list of one entry per day ahead, each with
    a cumulative `likelihood`: +2 means two rises projected, -1 one fall. The
    furthest-out entry is the one worth keeping, since it is the only one that
    says anything a manager could not read off today's price.

    New for 2026-27, and the reason it matters is that the mechanic changed
    underneath the market layer: prices used to move at most once a day, so
    inferring momentum from per-gameweek snapshots was the only option. They
    now move continuously and FPL publishes its own forecast.
    """
    projections = raw.get("price_change_projections")
    if not projections:
        return None
    furthest = max(projections, key=lambda entry: entry.get("offset", 0))
    likelihood = furthest.get("likelihood")
    return None if likelihood is None else int(likelihood)


def _optional_int(value: Any) -> int | None:
    """An absent field, versus one FPL reports as zero.

    Defensive Contribution and its components did not exist before 2025-26, and
    a zero would claim a player made no defensive actions where in fact nobody
    was counting.
    """
    if value is None or value == "":
        return None
    return int(value)


def upsert_teams(session: Session, teams_payload: list[dict[str, Any]]) -> dict[int, Team]:
    by_fpl_id = {t.fpl_id: t for t in session.query(Team).all()}
    for raw in teams_payload:
        team = by_fpl_id.get(raw["id"])
        if team is None:
            team = Team(fpl_id=raw["id"])
            session.add(team)
            by_fpl_id[raw["id"]] = team
        team.name = raw["name"]
        team.short_name = raw["short_name"]
        team.strength_overall_home = raw["strength_overall_home"]
        team.strength_overall_away = raw["strength_overall_away"]
        team.strength_attack_home = raw["strength_attack_home"]
        team.strength_attack_away = raw["strength_attack_away"]
        team.strength_defence_home = raw["strength_defence_home"]
        team.strength_defence_away = raw["strength_defence_away"]
    session.flush()
    return by_fpl_id


def upsert_players(
    session: Session, elements_payload: list[dict[str, Any]], teams_by_fpl_id: dict[int, Team]
) -> dict[int, Player]:
    by_fpl_id = {p.fpl_id: p for p in session.query(Player).all()}
    for raw in elements_payload:
        player = by_fpl_id.get(raw["id"])
        if player is None:
            player = Player(fpl_id=raw["id"])
            session.add(player)
            by_fpl_id[raw["id"]] = player
        player.team_id = teams_by_fpl_id[raw["team"]].id
        player.first_name = raw["first_name"]
        player.second_name = raw["second_name"]
        player.web_name = raw["web_name"]
        player.code = raw.get("code")
        player.element_type = raw["element_type"]
        player.now_cost = raw["now_cost"]
        player.selected_by_percent = as_float(raw["selected_by_percent"])
        player.form = as_float(raw["form"])
        player.ep_next = as_float(raw.get("ep_next"))
        player.total_points = raw["total_points"]
        player.status = raw["status"]
        player.chance_of_playing_next_round = raw["chance_of_playing_next_round"]
        player.news = raw.get("news", "") or ""
        player.birth_date = _parse_birth_date(raw.get("birth_date"))
        player.defensive_contribution_per_90 = as_float(raw.get("defensive_contribution_per_90"))
        player.price_change_percent = as_float(raw.get("price_change_percent"))
        player.price_change_hourly_rate = as_float(raw.get("price_change_hourly_rate"))
        player.price_change_likelihood = _price_change_likelihood(raw)
        player.updated_at = dt.datetime.now(dt.UTC)
    session.flush()
    return by_fpl_id


def upsert_fixtures(
    session: Session, fixtures_payload: list[dict[str, Any]], teams_by_fpl_id: dict[int, Team]
) -> None:
    by_fpl_id = {f.fpl_id: f for f in session.query(Fixture).all()}
    for raw in fixtures_payload:
        fixture = by_fpl_id.get(raw["id"])
        if fixture is None:
            fixture = Fixture(fpl_id=raw["id"])
            session.add(fixture)
            by_fpl_id[raw["id"]] = fixture
        fixture.event = raw["event"]
        fixture.kickoff_time = _parse_kickoff(raw["kickoff_time"])
        # FPL only flips `finished` once bonus points are confirmed, which can
        # lag the final whistle by a day or more; `finished_provisional` goes
        # true as soon as the match ends. Treat either as played, otherwise a
        # just-completed gameweek still looks upcoming and every "next fixture"
        # lookup resolves to a match that has already been played.
        fixture.finished = bool(raw["finished"] or raw.get("finished_provisional"))
        fixture.team_h_id = teams_by_fpl_id[raw["team_h"]].id
        fixture.team_a_id = teams_by_fpl_id[raw["team_a"]].id
        fixture.team_h_score = raw["team_h_score"]
        fixture.team_a_score = raw["team_a_score"]
        fixture.team_h_difficulty = raw["team_h_difficulty"]
        fixture.team_a_difficulty = raw["team_a_difficulty"]
    session.flush()


def upsert_player_gameweek_stats(
    session: Session, player: Player, history_payload: list[dict[str, Any]]
) -> None:
    # Keyed on (round, fixture), not on the round alone. FPL returns one entry
    # per match played, so in a double gameweek two entries share a round — and
    # a round-only key does not merge them, it overwrites: the second match's
    # row lands on top of the first and that match's minutes, goals and xG are
    # gone with no error raised. Doubles carried between 2.6% and 11% of all
    # player-minutes across the last four seasons.
    existing_by_fixture = {
        (s.round, s.fixture_fpl_id): s
        for s in session.query(PlayerGameweekStat).filter_by(player_id=player.id).all()
    }
    for raw in history_payload:
        key = (raw["round"], raw["fixture"])
        stat = existing_by_fixture.get(key)
        if stat is None:
            stat = PlayerGameweekStat(
                player_id=player.id, round=raw["round"], fixture_fpl_id=raw["fixture"]
            )
            session.add(stat)
            existing_by_fixture[key] = stat
        stat.opponent_team_fpl_id = raw["opponent_team"]
        stat.was_home = raw["was_home"]
        stat.kickoff_time = _parse_kickoff(raw["kickoff_time"])
        stat.minutes = raw["minutes"]
        stat.starts = raw.get("starts")
        stat.total_points = raw["total_points"]
        stat.goals_scored = raw["goals_scored"]
        stat.assists = raw["assists"]
        stat.clean_sheets = raw["clean_sheets"]
        stat.goals_conceded = raw["goals_conceded"]
        stat.bonus = raw["bonus"]
        stat.bps = raw["bps"]
        stat.influence = as_float(raw["influence"])
        stat.creativity = as_float(raw["creativity"])
        stat.threat = as_float(raw["threat"])
        stat.ict_index = as_float(raw["ict_index"])
        stat.expected_goals = as_float(raw.get("expected_goals"))
        stat.expected_assists = as_float(raw.get("expected_assists"))
        stat.expected_goal_involvements = as_float(raw.get("expected_goal_involvements"))
        stat.expected_goals_conceded = as_float(raw.get("expected_goals_conceded"))
        # Left as None where FPL doesn't publish them, so a season played
        # before the Defensive Contribution rule existed is distinguishable
        # from a player who simply made no defensive actions.
        stat.defensive_contribution = _optional_int(raw.get("defensive_contribution"))
        stat.clearances_blocks_interceptions = _optional_int(
            raw.get("clearances_blocks_interceptions")
        )
        stat.recoveries = _optional_int(raw.get("recoveries"))
        stat.tackles = _optional_int(raw.get("tackles"))
        stat.value = raw["value"]
        stat.selected = raw["selected"]
        stat.transfers_in = raw["transfers_in"]
        stat.transfers_out = raw["transfers_out"]


def next_unplayed_event(session: Session) -> int | None:
    """The gameweek a snapshot taken right now describes the run-up to.

    Read off the fixture list rather than counted forward, so a round that has
    been wiped out never appears and a part-played one still does. Recorded on
    the snapshot itself so a backtest can ask for "the state going into GW5"
    without re-deriving it from a calendar that will have moved on by then.
    """
    events = (
        session.query(Fixture.event)
        .filter(Fixture.finished.is_(False), Fixture.event.isnot(None))
        .distinct()
        .all()
    )
    return min((event for (event,) in events), default=None)


def _record_player_snapshots(
    session: Session, captured_at: dt.datetime, captured_on: dt.date, next_event: int | None
) -> int:
    existing = {
        snapshot.player_id: snapshot
        for snapshot in session.query(PlayerSnapshot).filter(
            PlayerSnapshot.captured_on == captured_on
        )
    }
    for player in session.query(Player).all():
        snapshot = existing.get(player.id)
        if snapshot is None:
            snapshot = PlayerSnapshot(player_id=player.id, captured_on=captured_on)
            session.add(snapshot)
        snapshot.captured_at = captured_at
        snapshot.next_event = next_event
        snapshot.now_cost = player.now_cost
        snapshot.ep_next = player.ep_next
        snapshot.form = player.form
        snapshot.selected_by_percent = player.selected_by_percent
        snapshot.status = player.status
        snapshot.chance_of_playing_next_round = player.chance_of_playing_next_round
        snapshot.news = player.news
    return len(session.query(Player).all())


def _record_team_snapshots(
    session: Session, captured_at: dt.datetime, captured_on: dt.date, next_event: int | None
) -> int:
    existing = {
        snapshot.team_id: snapshot
        for snapshot in session.query(TeamSnapshot).filter(TeamSnapshot.captured_on == captured_on)
    }
    teams = session.query(Team).all()
    for team in teams:
        snapshot = existing.get(team.id)
        if snapshot is None:
            snapshot = TeamSnapshot(team_id=team.id, captured_on=captured_on)
            session.add(snapshot)
        snapshot.captured_at = captured_at
        snapshot.next_event = next_event
        snapshot.strength_overall_home = team.strength_overall_home
        snapshot.strength_overall_away = team.strength_overall_away
        snapshot.strength_attack_home = team.strength_attack_home
        snapshot.strength_attack_away = team.strength_attack_away
        snapshot.strength_defence_home = team.strength_defence_home
        snapshot.strength_defence_away = team.strength_defence_away
    return len(teams)


def record_snapshots(session: Session, captured_at: dt.datetime | None = None) -> int:
    """Archive today's overwritable player and team fields. Returns rows written.

    Called at the end of every ingest, so the existing daily cron collects this
    with no change to the schedule. Upserted on the day rather than appended,
    so running the ingest twice in an afternoon updates that day's row instead
    of doubling the table — the last read before a deadline is the one that
    matters, and prices only move once a day anyway.

    Deliberately best-effort at the call site: this is instrumentation for a
    backtest that does not exist yet, and it must never be the reason a data
    refresh fails.
    """
    captured_at = captured_at or dt.datetime.now(dt.UTC)
    captured_on = captured_at.date()
    next_event = next_unplayed_event(session)

    written = _record_player_snapshots(session, captured_at, captured_on, next_event)
    written += _record_team_snapshots(session, captured_at, captured_on, next_event)
    session.flush()
    return written


def run_ingest(client: FPLClient | None = None, sleep_between_requests: float = 0.1) -> None:
    """Full pull: teams, players, fixtures, and per-player gameweek history."""
    owns_client = client is None
    client = client or FPLClient()
    try:
        with session_scope() as session:
            bootstrap = client.get_bootstrap_static()
            teams_by_fpl_id = upsert_teams(session, bootstrap["teams"])
            players_by_fpl_id = upsert_players(session, bootstrap["elements"], teams_by_fpl_id)
            upsert_fixtures(session, client.get_fixtures(), teams_by_fpl_id)

            total = len(players_by_fpl_id)
            for i, (fpl_id, player) in enumerate(players_by_fpl_id.items(), start=1):
                summary = client.get_element_summary(fpl_id)
                upsert_player_gameweek_stats(session, player, summary["history"])
                if i % 50 == 0 or i == total:
                    logger.info("Fetched gameweek history for %d/%d players", i, total)
                time.sleep(sleep_between_requests)

            # Archive the fields this ingest just overwrote. Wrapped because
            # it is instrumentation for a backtest that doesn't exist yet:
            # losing a day of snapshots is a nuisance, losing the refresh that
            # every other feature depends on is not.
            try:
                written = record_snapshots(session)
                logger.info("Recorded %d point-in-time snapshots", written)
            except Exception:
                logger.exception("Failed to record snapshots; ingest itself is unaffected")

            warn_if_stale(session)
    finally:
        if owns_client:
            client.close()


def warn_if_stale(session: Session) -> int | None:
    """Warn when gameweek history lags the fixtures that have been played.

    A silently stale database is the failure mode this pipeline has no other
    defence against. Every projection it produces still looks entirely
    reasonable — the engine has data, it is just last week's — so nothing
    downstream can tell, and a cron that stopped firing looks exactly like a
    quiet week. Returns the number of gameweeks behind, or None if it is
    current.
    """
    latest_played = (
        session.query(Fixture.event)
        .filter(Fixture.finished.is_(True), Fixture.event.isnot(None))
        .order_by(Fixture.event.desc())
        .limit(1)
        .scalar()
    )
    latest_ingested = (
        session.query(PlayerGameweekStat.round)
        .order_by(PlayerGameweekStat.round.desc())
        .limit(1)
        .scalar()
    )
    if latest_played is None:
        return None
    behind = int(latest_played) - int(latest_ingested or 0)
    if behind <= 0:
        return None
    logger.warning(
        "Gameweek history is %d gameweek(s) behind: fixtures are finished through GW%s but "
        "player stats stop at GW%s. Predictions will be built on stale data.",
        behind,
        latest_played,
        latest_ingested if latest_ingested is not None else "none",
    )
    return behind


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_ingest()


if __name__ == "__main__":
    main()
