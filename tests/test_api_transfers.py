from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import fplquant.api.routers.transfers as transfers_router
from fplquant.models.orm import Player, Team
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER

PAST_EVENT = {"id": 1, "deadline_time": "2020-01-01T00:00:00Z"}
FUTURE_EVENT = {"id": 2, "deadline_time": "2099-01-01T00:00:00Z"}


class StubFPLClient:
    def __init__(
        self, bootstrap: dict[str, Any], entry: dict[str, Any], picks: dict[str, Any]
    ) -> None:
        self._bootstrap = bootstrap
        self._entry = entry
        self._picks = picks

    def get_bootstrap_static(self) -> dict[str, Any]:
        return self._bootstrap

    def get_entry(self, team_id: int) -> dict[str, Any]:
        return self._entry

    def get_entry_picks(self, team_id: int, event_id: int) -> dict[str, Any]:
        return self._picks

    def close(self) -> None:
        pass

    def __enter__(self) -> "StubFPLClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _seed_squad(session: Session) -> list[int]:
    """A valid 15-man squad (2 GKP/5 DEF/5 MID/3 FWD), one team each to keep
    club limits out of the way, plus a wider replacement pool with clearly
    better ep_next so transfer recommendations have something to bite on."""
    positions = [GOALKEEPER] * 2 + [DEFENDER] * 5 + [MIDFIELDER] * 5 + [FORWARD] * 3
    fpl_id = 1
    squad_fpl_ids = []
    for i, position in enumerate(positions):
        team = Team(fpl_id=1000 + i, name=f"SquadTeam{i}", short_name=f"S{i}")
        session.add(team)
        session.flush()
        session.add(
            Player(
                fpl_id=fpl_id,
                team_id=team.id,
                first_name=f"P{fpl_id}",
                second_name=f"P{fpl_id}",
                web_name=f"P{fpl_id}",
                element_type=position,
                now_cost=50,
                status="a",
                ep_next=4.0,
            )
        )
        session.flush()
        squad_fpl_ids.append(fpl_id)
        fpl_id += 1

    for i, position in enumerate([GOALKEEPER, DEFENDER, MIDFIELDER, FORWARD] * 3):
        team = Team(fpl_id=2000 + i, name=f"PoolTeam{i}", short_name=f"X{i}")
        session.add(team)
        session.flush()
        session.add(
            Player(
                fpl_id=fpl_id,
                team_id=team.id,
                first_name=f"U{fpl_id}",
                second_name=f"U{fpl_id}",
                web_name=f"U{fpl_id}",
                element_type=position,
                now_cost=50,
                status="a",
                ep_next=20.0,  # a clear upgrade over anyone in the seeded squad
            )
        )
        session.flush()
        fpl_id += 1

    session.commit()
    return squad_fpl_ids


def _picks_payload(squad_fpl_ids: list[int], bank: int = 0) -> dict[str, Any]:
    return {
        "picks": [{"element": fid, "position": i + 1} for i, fid in enumerate(squad_fpl_ids)],
        "entry_history": {"bank": bank},
    }


def test_plan_transfers_returns_current_squad_and_recommends_upgrades(
    db_session: Session, api_client: TestClient, monkeypatch: Any
) -> None:
    squad_fpl_ids = _seed_squad(db_session)
    stub = StubFPLClient(
        bootstrap={"events": [PAST_EVENT]},
        entry={"name": "My Team"},
        picks=_picks_payload(squad_fpl_ids),
    )
    monkeypatch.setattr(transfers_router, "FPLClient", lambda: stub)

    response = api_client.post("/transfers/plan", json={"fpl_team_id": 123, "free_transfers": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["team_name"] == "My Team"
    assert body["event_id"] == 1
    assert len(body["current_squad"]) == 15
    assert body["transfers_made"] >= 1
    assert body["worth_it"] is True
    # Every replacement is a big enough upgrade (ep_next 20 vs 4) that even
    # paying repeated -4 hits beyond the 1 free transfer is worth it.
    assert body["points_gain_after_hit"] > 0
    assert len(body["starting_xi"]["starters"]) == 11


def test_plan_transfers_before_season_start_returns_400(
    db_session: Session, api_client: TestClient, monkeypatch: Any
) -> None:
    _seed_squad(db_session)
    stub = StubFPLClient(bootstrap={"events": [FUTURE_EVENT]}, entry={}, picks={})
    monkeypatch.setattr(transfers_router, "FPLClient", lambda: stub)

    response = api_client.post("/transfers/plan", json={"fpl_team_id": 123})

    assert response.status_code == 400
    assert "detail" in response.json()


def test_plan_transfers_wildcard_ignores_hit_cost(
    db_session: Session, api_client: TestClient, monkeypatch: Any
) -> None:
    squad_fpl_ids = _seed_squad(db_session)
    stub = StubFPLClient(
        bootstrap={"events": [PAST_EVENT]},
        entry={"name": "My Team"},
        picks=_picks_payload(squad_fpl_ids),
    )
    monkeypatch.setattr(transfers_router, "FPLClient", lambda: stub)

    response = api_client.post(
        "/transfers/plan", json={"fpl_team_id": 123, "free_transfers": 0, "chip": "wildcard"}
    )

    body = response.json()
    assert response.status_code == 200
    assert body["chip"] == "wildcard"
    assert body["hit_cost"] == 0
    assert body["transfers_made"] > 1  # more than a single free transfer would allow


def test_plan_transfers_invalid_chip_returns_422(
    db_session: Session, api_client: TestClient
) -> None:
    response = api_client.post(
        "/transfers/plan", json={"fpl_team_id": 123, "chip": "not_a_real_chip"}
    )
    assert response.status_code == 422


def test_plan_transfers_risk_adjusted_flag_is_accepted(
    db_session: Session, api_client: TestClient, monkeypatch: Any
) -> None:
    squad_fpl_ids = _seed_squad(db_session)
    stub = StubFPLClient(
        bootstrap={"events": [PAST_EVENT]},
        entry={"name": "My Team"},
        picks=_picks_payload(squad_fpl_ids),
    )
    monkeypatch.setattr(transfers_router, "FPLClient", lambda: stub)

    response = api_client.post(
        "/transfers/plan",
        json={"fpl_team_id": 123, "risk_adjusted": True, "risk_aversion": 2.0},
    )

    assert response.status_code == 200
    assert len(response.json()["current_squad"]) == 15


def _seed_league_and_pick_a_squad(session: Session) -> list[int]:
    """A league with fixtures ahead of it, and a legal fifteen to own."""
    from tests.engine_helpers import make_league, make_round

    teams = make_league(session, teams=8)
    for event in (1, 2, 3):
        make_round(session, teams, event)
    session.commit()

    picked: list[int] = []
    per_club: dict[int, int] = {}
    for position, needed in ((GOALKEEPER, 2), (DEFENDER, 5), (MIDFIELDER, 5), (FORWARD, 3)):
        taken = 0
        for player in (
            session.query(Player)
            .filter(Player.element_type == position)
            .order_by(Player.fpl_id)
            .all()
        ):
            if taken == needed:
                break
            if per_club.get(player.team_id, 0) >= 3:
                continue
            per_club[player.team_id] = per_club.get(player.team_id, 0) + 1
            picked.append(player.fpl_id)
            taken += 1
        assert taken == needed, f"only {taken} of {needed} for position {position}"
    assert len(picked) == 15
    return picked


def test_plan_transfers_searches_the_horizon_and_offers_alternatives(
    db_session: Session, api_client: TestClient, monkeypatch: Any
) -> None:
    """The endpoint answers with ranked lines from the current position, and
    holding is one of them — a manager is owed the value of doing nothing."""
    squad_fpl_ids = _seed_league_and_pick_a_squad(db_session)
    stub = StubFPLClient(
        bootstrap={"events": [PAST_EVENT]},
        entry={"name": "My Team"},
        picks=_picks_payload(squad_fpl_ids),
    )
    monkeypatch.setattr(transfers_router, "FPLClient", lambda: stub)

    response = api_client.post(
        "/transfers/plan", json={"fpl_team_id": 123, "free_transfers": 1, "horizon": 3, "lines": 2}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["horizon_events"] == [1, 2, 3]
    assert body["lines"]
    assert any(line["is_hold"] for line in body["lines"])
    # Ranked best first, on the engine's own evaluation.
    gains = [line["gain_vs_hold"] for line in body["lines"]]
    assert gains == sorted(gains, reverse=True)
    assert body["lines_searched"] >= 2


def test_plan_transfers_answers_for_one_week_when_there_is_no_horizon(
    db_session: Session, api_client: TestClient, monkeypatch: Any
) -> None:
    """Preseason, or a database part-way through its first ingest: there is no
    fixture to search over. That is not a reason to refuse a question the
    single-gameweek path can still answer."""
    squad_fpl_ids = _seed_squad(db_session)  # players, no fixtures
    stub = StubFPLClient(
        bootstrap={"events": [PAST_EVENT]},
        entry={"name": "My Team"},
        picks=_picks_payload(squad_fpl_ids),
    )
    monkeypatch.setattr(transfers_router, "FPLClient", lambda: stub)

    response = api_client.post("/transfers/plan", json={"fpl_team_id": 123, "free_transfers": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["horizon_events"] == []
    assert body["lines"] == []
    assert body["resulting_squad"]
