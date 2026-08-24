import datetime as dt
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from fplquant.data.fpl_client import FPLClient
from fplquant.models.base import session_scope
from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team
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
        fixture.finished = raw["finished"]
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
    existing_by_round = {
        s.round: s for s in session.query(PlayerGameweekStat).filter_by(player_id=player.id).all()
    }
    for raw in history_payload:
        stat = existing_by_round.get(raw["round"])
        if stat is None:
            stat = PlayerGameweekStat(player_id=player.id, round=raw["round"])
            session.add(stat)
            existing_by_round[raw["round"]] = stat
        stat.fixture_fpl_id = raw["fixture"]
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
        stat.value = raw["value"]
        stat.selected = raw["selected"]
        stat.transfers_in = raw["transfers_in"]
        stat.transfers_out = raw["transfers_out"]


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
    finally:
        if owns_client:
            client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_ingest()


if __name__ == "__main__":
    main()
