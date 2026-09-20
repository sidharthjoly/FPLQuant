from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fplquant.api import cache as cache_module
from fplquant.api.routers.optimizer import _cache_key
from fplquant.api.schemas import OptimizeRequest
from fplquant.models.orm import Player, Team
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER


def _seed_full_pool(session: Session, num_teams: int = 6, per_team_per_position: int = 3) -> None:
    positions = (GOALKEEPER, DEFENDER, MIDFIELDER, FORWARD)
    fpl_id = 1
    for team_index in range(num_teams):
        team = Team(fpl_id=team_index + 1, name=f"Team{team_index}", short_name=f"T{team_index}")
        session.add(team)
        session.flush()
        for position in positions:
            for _ in range(per_team_per_position):
                session.add(
                    Player(
                        fpl_id=fpl_id,
                        team_id=team.id,
                        first_name=f"P{fpl_id}",
                        second_name=f"P{fpl_id}",
                        web_name=f"P{fpl_id}",
                        element_type=position,
                        now_cost=40,
                        status="a",
                        ep_next=3.0,
                    )
                )
                fpl_id += 1
    session.commit()


def test_optimize_returns_valid_squad(db_session: Session, api_client: TestClient) -> None:
    _seed_full_pool(db_session)

    response = api_client.post("/optimize", json={"budget": 100.0, "max_per_club": 3})

    assert response.status_code == 200
    body = response.json()
    assert len(body["squad"]) == 15
    assert body["total_cost"] <= 1000


def test_optimize_includes_starting_xi(db_session: Session, api_client: TestClient) -> None:
    _seed_full_pool(db_session)

    response = api_client.post("/optimize", json={"budget": 100.0, "max_per_club": 3})

    body = response.json()
    xi = body["starting_xi"]
    assert len(xi["starters"]) == 11
    assert len(xi["bench"]) == 4
    assert xi["captain"]["player_id"] in [p["player_id"] for p in xi["starters"]]
    assert xi["vice_captain"]["player_id"] in [p["player_id"] for p in xi["starters"]]
    assert xi["captain"]["player_id"] != xi["vice_captain"]["player_id"]
    d, m, f = (int(part) for part in xi["formation"].split("-"))
    assert d + m + f == 10
    assert xi["bench_boost_value"] >= 0
    assert xi["triple_captain_value"] == xi["captain"]["predicted_points"]


def test_optimize_accepts_a_forced_formation(db_session: Session, api_client: TestClient) -> None:
    _seed_full_pool(db_session)

    response = api_client.post(
        "/optimize", json={"budget": 100.0, "max_per_club": 3, "formation": "4-4-2"}
    )

    assert response.status_code == 200
    assert response.json()["starting_xi"]["formation"] == "4-4-2"


def test_optimize_rejects_an_invalid_formation(db_session: Session, api_client: TestClient) -> None:
    _seed_full_pool(db_session)

    response = api_client.post(
        "/optimize", json={"budget": 100.0, "max_per_club": 3, "formation": "9-9-9"}
    )

    assert response.status_code == 422


def test_optimize_infeasible_returns_400(db_session: Session, api_client: TestClient) -> None:
    _seed_full_pool(db_session)

    response = api_client.post("/optimize", json={"budget": 1.0, "max_per_club": 3})

    assert response.status_code == 400
    assert "detail" in response.json()


def test_optimize_risk_adjusted_flag_is_accepted(
    db_session: Session, api_client: TestClient
) -> None:
    _seed_full_pool(db_session)

    response = api_client.post(
        "/optimize",
        json={"budget": 100.0, "max_per_club": 3, "risk_adjusted": True, "risk_aversion": 2.0},
    )

    assert response.status_code == 200
    assert len(response.json()["squad"]) == 15


def test_optimize_result_is_cached_in_redis(db_session: Session, api_client: TestClient) -> None:
    _seed_full_pool(db_session)
    request = {"budget": 100.0, "max_per_club": 3}

    response = api_client.post("/optimize", json=request)
    assert response.status_code == 200

    key = _cache_key(OptimizeRequest(**request))
    cached_value = cache_module.get_client().get(key)
    assert cached_value is not None


def test_optimize_second_identical_request_returns_cached_response(
    db_session: Session, api_client: TestClient
) -> None:
    _seed_full_pool(db_session)
    request = {"budget": 100.0, "max_per_club": 3}

    first = api_client.post("/optimize", json=request).json()
    second = api_client.post("/optimize", json=request).json()

    assert first == second


def test_optimize_recovers_from_a_stale_cache_entry(
    db_session: Session, api_client: TestClient
) -> None:
    """Reproduces a real production incident: a cache entry written before a
    response-schema change (a new required field) must not 500 the request
    — it should be treated as a cache miss, recomputed, and the stale entry
    overwritten."""
    _seed_full_pool(db_session)
    request = {"budget": 100.0, "max_per_club": 3}

    key = _cache_key(OptimizeRequest(**request))
    stale_payload = '{"total_cost": 900, "total_predicted_points": 30.0, "squad": []}'
    cache_module.get_client().set(key, stale_payload, ex=3600)

    response = api_client.post("/optimize", json=request)

    assert response.status_code == 200
    assert "starting_xi" in response.json()

    # The stale entry should have been overwritten with a valid one.
    refreshed = cache_module.get_client().get(key)
    assert "starting_xi" in refreshed


def test_two_projections_do_not_share_a_cache_entry() -> None:
    """They are different answers to the same question, so they must not be
    able to be served for one another."""
    engine = _cache_key(OptimizeRequest(projection="engine"))
    form = _cache_key(OptimizeRequest(projection="form"))
    assert engine != form


def test_the_cache_key_changed_shape_when_the_default_projection_did() -> None:
    """Redis outlives a deploy. Entries written before `projection` existed
    hold form-path squads for what is now an engine-path request, and without
    a version in the key the first call after the swap serves one back and the
    change looks like it did nothing."""
    assert _cache_key(OptimizeRequest()).startswith("fplquant:optimize:v2:")


def test_optimize_defaults_to_the_engine_projection(
    db_session: Session, api_client: TestClient
) -> None:
    """The measured default: replaying 26/27, the same solver under the same
    constraints scored thirteen points a week higher on the engine path."""
    _seed_full_pool(db_session)

    response = api_client.post("/optimize", json={"budget": 100.0})

    assert response.status_code == 200
    assert OptimizeRequest().projection == "engine"


def test_optimize_still_accepts_the_form_projection(
    db_session: Session, api_client: TestClient
) -> None:
    """Kept so the two can be compared on live data, not for daily use."""
    _seed_full_pool(db_session)

    response = api_client.post("/optimize", json={"budget": 100.0, "projection": "form"})

    assert response.status_code == 200
    assert len(response.json()["squad"]) == 15


def test_optimize_rejects_a_projection_it_does_not_have(
    db_session: Session, api_client: TestClient
) -> None:
    _seed_full_pool(db_session)

    response = api_client.post("/optimize", json={"budget": 100.0, "projection": "vibes"})

    assert response.status_code == 422
