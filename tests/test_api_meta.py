import datetime as dt
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import fplquant.api.routers.meta as meta_router
from tests.engine_helpers import make_fixture, make_league


class StubFPLClient:
    def __init__(self, bootstrap: dict[str, Any]) -> None:
        self._bootstrap = bootstrap

    def get_bootstrap_static(self) -> dict[str, Any]:
        return self._bootstrap

    def close(self) -> None:
        pass

    def __enter__(self) -> "StubFPLClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _deadline(days_from_now: float) -> str:
    """A deadline relative to now, in FPL's wire format.

    Relative rather than a fixed date, because the endpoint's whole job is to
    compare against the clock. Hardcoded dates made this test pass until the
    real world caught up with them and the "upcoming" gameweek quietly became a
    past one — the assertion then failed with the code behaving correctly.
    """
    moment = dt.datetime.now(dt.UTC) + dt.timedelta(days=days_from_now)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_next_deadline_picks_the_earliest_unfinished_event(
    api_client: TestClient, monkeypatch: Any
) -> None:
    passed, soon, later = _deadline(-7), _deadline(3), _deadline(10)
    stub = StubFPLClient(
        bootstrap={
            "events": [
                {"id": 1, "finished": True, "deadline_time": passed},
                {"id": 3, "finished": False, "deadline_time": later},
                {"id": 2, "finished": False, "deadline_time": soon},
            ]
        }
    )
    monkeypatch.setattr(meta_router, "FPLClient", lambda: stub)

    response = api_client.get("/meta/next-deadline")

    assert response.status_code == 200
    body = response.json()
    assert body["gameweek"] == 2
    assert body["deadline"] == soon


def test_next_deadline_returns_null_when_season_is_over(
    api_client: TestClient, monkeypatch: Any
) -> None:
    stub = StubFPLClient(
        bootstrap={"events": [{"id": 1, "finished": True, "deadline_time": _deadline(-7)}]}
    )
    monkeypatch.setattr(meta_router, "FPLClient", lambda: stub)

    response = api_client.get("/meta/next-deadline")

    assert response.status_code == 200
    assert response.json() == {"deadline": None, "gameweek": None}


def test_remaining_gameweeks_counts_only_rounds_with_football_left(
    db_session: Session, api_client: TestClient
) -> None:
    """The planner clamps its horizon to this, so the dashboard needs it to
    offer horizons that still mean something rather than ones that silently
    collapse to something shorter."""
    teams = make_league(db_session, teams=4)
    make_fixture(
        db_session, teams[0], teams[1], fpl_id=1, event=1, finished=True, home_score=1, away_score=0
    )
    make_fixture(db_session, teams[0], teams[1], fpl_id=2, event=4)
    make_fixture(db_session, teams[2], teams[3], fpl_id=3, event=7)
    db_session.commit()

    body = api_client.get("/meta/remaining-gameweeks").json()

    assert body["events"] == [4, 7]
    assert body["count"] == 2


def test_remaining_gameweeks_is_zero_once_the_season_is_done(
    db_session: Session, api_client: TestClient
) -> None:
    teams = make_league(db_session, teams=2)
    make_fixture(
        db_session,
        teams[0],
        teams[1],
        fpl_id=1,
        event=38,
        finished=True,
        home_score=2,
        away_score=2,
    )
    db_session.commit()

    body = api_client.get("/meta/remaining-gameweeks").json()

    assert body["count"] == 0
    assert body["events"] == []
