from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.engine_helpers import make_fixture, make_league, make_round


def _seed(session: Session, teams: int = 8, rounds: int = 3) -> None:
    built = make_league(session, teams=teams)
    for event in range(1, rounds + 1):
        make_round(session, built, event)
    session.commit()


def test_projections_return_a_scoring_breakdown_per_fixture(
    db_session: Session, api_client: TestClient
) -> None:
    _seed(db_session)

    response = api_client.get("/projections?horizon=3&limit=5")

    assert response.status_code == 200
    body = response.json()
    assert body["events"] == [1, 2, 3]
    assert len(body["players"]) == 5
    player = body["players"][0]
    assert player["discounted_points"] <= player["total_points"]
    breakdown = player["events"][0]["fixtures"][0]["breakdown"]
    assert breakdown["total"] > 0
    assert 0.0 <= breakdown["clean_sheet_probability"] <= 1.0


def test_projections_are_ordered_best_first(db_session: Session, api_client: TestClient) -> None:
    _seed(db_session)

    body = api_client.get("/projections?horizon=2&limit=20").json()

    scores = [player["discounted_points"] for player in body["players"]]
    assert scores == sorted(scores, reverse=True)


def test_projections_can_simulate_the_next_full_gameweek(
    db_session: Session, api_client: TestClient
) -> None:
    _seed(db_session)

    body = api_client.get(
        "/projections?horizon=2&limit=3&simulate=true&simulations=500&seed=1"
    ).json()

    assert body["simulated_event"] == 1
    assert body["simulations"] == 500
    outcome = body["players"][0]["outcome"]
    assert outcome["floor"] <= outcome["median"] <= outcome["ceiling"]


def test_projections_report_a_blank_gameweek(db_session: Session, api_client: TestClient) -> None:
    teams = make_league(db_session, teams=4)
    make_fixture(db_session, teams[0], teams[1], fpl_id=1, event=1)
    make_fixture(db_session, teams[2], teams[3], fpl_id=2, event=2)
    db_session.commit()

    body = api_client.get("/projections?horizon=2&limit=100").json()

    blanking = next(p for p in body["players"] if p["team_id"] == teams[2].id)
    assert blanking["events"][0]["is_blank"] is True
    assert blanking["events"][0]["points"] == 0.0


def test_plan_returns_a_gameweek_by_gameweek_plan(
    db_session: Session, api_client: TestClient
) -> None:
    _seed(db_session)

    response = api_client.post("/plan", json={"horizon": 2, "budget": 100.0})

    assert response.status_code == 200
    body = response.json()
    assert [gameweek["event"] for gameweek in body["gameweeks"]] == [1, 2]
    first = body["gameweeks"][0]
    assert len(first["squad"]) == 15
    assert len(first["starting_xi"]["starters"]) == 11
    assert first["starting_xi"]["captain"]["player_id"] is not None


def test_plan_can_schedule_a_chip(db_session: Session, api_client: TestClient) -> None:
    _seed(db_session)

    body = api_client.post(
        "/plan", json={"horizon": 3, "budget": 100.0, "chips": ["triple_captain"]}
    ).json()

    chips = [gameweek["chip"] for gameweek in body["gameweeks"]]
    assert chips.count("triple_captain") <= 1


def test_plan_rejects_an_unknown_chip(db_session: Session, api_client: TestClient) -> None:
    response = api_client.post("/plan", json={"chips": ["assistant_manager"]})
    assert response.status_code == 422


def test_an_unaffordable_budget_is_a_client_error(
    db_session: Session, api_client: TestClient
) -> None:
    """`InfeasibleSquadError` is a bad request, not a 500 — the app-level
    handler already makes that call and this route has to route through it."""
    _seed(db_session)

    response = api_client.post("/plan", json={"horizon": 1, "budget": 1.0})

    assert response.status_code == 400
    assert "feasible" in response.json()["detail"].lower()


def test_a_projection_response_is_served_from_cache_the_second_time(
    db_session: Session, api_client: TestClient
) -> None:
    _seed(db_session)

    first = api_client.get("/projections?horizon=2&limit=3").json()
    second = api_client.get("/projections?horizon=2&limit=3").json()

    assert first == second
