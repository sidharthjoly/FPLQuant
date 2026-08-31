import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fplquant.models.orm import Fixture, Player, PlayerGameweekStat, Team


def _team(session: Session, fpl_id: int = 1) -> Team:
    team = Team(fpl_id=fpl_id, name="Arsenal", short_name="ARS")
    session.add(team)
    session.flush()
    return team


def _player(
    session: Session,
    team: Team,
    fpl_id: int,
    web_name: str,
    element_type: int = 4,
    first_name: str | None = None,
    second_name: str | None = None,
    selected_by_percent: float = 0.0,
    code: int | None = None,
    nationality: str | None = None,
) -> Player:
    player = Player(
        fpl_id=fpl_id,
        team_id=team.id,
        first_name=first_name if first_name is not None else web_name,
        second_name=second_name if second_name is not None else web_name,
        web_name=web_name,
        element_type=element_type,
        now_cost=80,
        status="a",
        ep_next=5.0,
        selected_by_percent=selected_by_percent,
        code=code,
        nationality=nationality,
    )
    session.add(player)
    session.flush()
    return player


def test_list_players_returns_seeded_players(db_session: Session, api_client: TestClient) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "Striker")
    db_session.commit()

    response = api_client.get("/players")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["web_name"] == "Striker"
    assert body[0]["team_short_name"] == "ARS"


def test_list_players_filters_by_position(db_session: Session, api_client: TestClient) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "Fwd", element_type=4)
    _player(db_session, team, 2, "Def", element_type=2)
    db_session.commit()

    response = api_client.get("/players", params={"position": 2})

    body = response.json()
    assert [p["web_name"] for p in body] == ["Def"]


def test_list_players_filters_by_search(db_session: Session, api_client: TestClient) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "Saka")
    _player(db_session, team, 2, "Gabriel")
    db_session.commit()

    response = api_client.get("/players", params={"search": "sak"})

    body = response.json()
    assert [p["web_name"] for p in body] == ["Saka"]


def test_search_matches_first_name_even_when_web_name_differs(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "Bruno G.", first_name="Bruno", second_name="Guimaraes")
    db_session.commit()

    response = api_client.get("/players", params={"search": "bruno"})

    body = response.json()
    assert [p["web_name"] for p in body] == ["Bruno G."]


def test_search_matches_last_name_even_when_web_name_differs(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "Bruno G.", first_name="Bruno", second_name="Guimaraes")
    db_session.commit()

    response = api_client.get("/players", params={"search": "guimaraes"})

    body = response.json()
    assert [p["web_name"] for p in body] == ["Bruno G."]


def test_search_is_accent_insensitive(db_session: Session, api_client: TestClient) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "Gyökeres", first_name="Viktor", second_name="Gyökeres")
    db_session.commit()

    response = api_client.get("/players", params={"search": "gyokeres"})

    body = response.json()
    assert [p["web_name"] for p in body] == ["Gyökeres"]


def test_search_ranks_exact_and_prefix_matches_above_substring_matches(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    # "Sam" substring-matches all three; only "Sam" itself is an exact match,
    # "Samuel" is a prefix match, "Osama" only contains it mid-string.
    _player(db_session, team, 1, "Osama")
    _player(db_session, team, 2, "Samuel")
    _player(db_session, team, 3, "Sam")
    db_session.commit()

    response = api_client.get("/players", params={"search": "sam"})

    body = response.json()
    assert [p["web_name"] for p in body] == ["Sam", "Samuel", "Osama"]


def test_search_matches_nothing_returns_empty_list(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "Saka")
    db_session.commit()

    response = api_client.get("/players", params={"search": "zzznomatch"})

    assert response.json() == []


def test_list_players_sorted_by_popularity(db_session: Session, api_client: TestClient) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "LowOwned", selected_by_percent=2.0)
    _player(db_session, team, 2, "TopOwned", selected_by_percent=45.0)
    _player(db_session, team, 3, "MidOwned", selected_by_percent=15.0)
    db_session.commit()

    response = api_client.get("/players", params={"sort": "popularity"})

    body = response.json()
    assert [p["web_name"] for p in body] == ["TopOwned", "MidOwned", "LowOwned"]


def test_list_players_limit_caps_results(db_session: Session, api_client: TestClient) -> None:
    team = _team(db_session)
    for i in range(5):
        _player(db_session, team, i + 1, f"P{i}")
    db_session.commit()

    response = api_client.get("/players", params={"limit": 2})

    assert len(response.json()) == 2


def test_list_players_rejects_invalid_sort(db_session: Session, api_client: TestClient) -> None:
    response = api_client.get("/players", params={"sort": "nonsense"})
    assert response.status_code == 422


def test_list_players_includes_full_name_nationality_and_photo(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    _player(
        db_session,
        team,
        1,
        "Saka",
        first_name="Bukayo",
        second_name="Saka",
        code=433177,
        nationality="England",
    )
    db_session.commit()

    response = api_client.get("/players")

    body = response.json()[0]
    assert body["full_name"] == "Bukayo Saka"
    assert body["nationality"] == "England"
    assert (
        body["photo_url"]
        == "https://resources.premierleague.com/premierleague/photos/players/250x250/p433177.png"
    )


def test_list_players_photo_url_and_nationality_are_none_when_unresolved(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    _player(db_session, team, 1, "NoData")
    db_session.commit()

    response = api_client.get("/players")

    body = response.json()[0]
    assert body["photo_url"] is None
    assert body["nationality"] is None


def test_get_player_404_when_missing(api_client: TestClient) -> None:
    response = api_client.get("/players/999")
    assert response.status_code == 404


def test_get_player_detail_includes_none_form_and_risk_when_no_history(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    player = _player(db_session, team, 1, "NoHistory")
    db_session.commit()

    response = api_client.get(f"/players/{player.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["web_name"] == "NoHistory"
    assert body["form_score"] is None
    assert body["injury_risk"] is not None  # always computable, unlike form


def test_get_player_detail_includes_form_when_history_exists(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    player = _player(db_session, team, 1, "HasHistory")
    for round_number in range(1, 4):
        db_session.add(PlayerGameweekStat(player_id=player.id, round=round_number, total_points=5))
    db_session.commit()

    response = api_client.get(f"/players/{player.id}")

    body = response.json()
    assert body["form_score"] is not None
    assert body["form_score"]["points_form"] == 5.0


def test_get_player_detail_includes_next_fixture_info(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session, fpl_id=1)
    opponent = Team(fpl_id=2, name="Chelsea", short_name="CHE")
    db_session.add(opponent)
    db_session.flush()
    player = _player(db_session, team, 1, "Home")
    db_session.add(
        Fixture(
            fpl_id=1,
            kickoff_time=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            finished=False,
            team_h_id=team.id,
            team_a_id=opponent.id,
            team_h_difficulty=2,
            team_a_difficulty=4,
        )
    )
    db_session.commit()

    response = api_client.get(f"/players/{player.id}")

    body = response.json()
    assert body["next_opponent"] == "CHE"
    assert body["next_opponent_is_home"] is True
    assert body["fixture_difficulty"] == 2
    assert body["chance_of_playing"] == 1.0


def test_get_player_history_404_when_missing(api_client: TestClient) -> None:
    response = api_client.get("/players/999/history")
    assert response.status_code == 404


def test_get_player_history_returns_gameweek_series(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    player = _player(db_session, team, 1, "Player")
    for round_number, pts in enumerate([2, 6, 10], start=1):
        db_session.add(
            PlayerGameweekStat(
                player_id=player.id,
                round=round_number,
                total_points=pts,
                minutes=90,
                value=80,
                selected=1000,
            )
        )
    db_session.commit()

    response = api_client.get(f"/players/{player.id}/history")

    assert response.status_code == 200
    body = response.json()
    assert [row["round"] for row in body] == [1, 2, 3]
    assert [row["total_points"] for row in body] == [2, 6, 10]


def test_get_similar_players_404_when_missing(api_client: TestClient) -> None:
    response = api_client.get("/players/999/similar")
    assert response.status_code == 404


def test_get_similar_players_includes_team_short_name(
    db_session: Session, api_client: TestClient
) -> None:
    team_a = _team(db_session, fpl_id=1)
    team_b = Team(fpl_id=2, name="Chelsea", short_name="CHE")
    db_session.add(team_b)
    db_session.flush()
    target = _player(db_session, team_a, 1, "Target", element_type=3)
    other = _player(db_session, team_b, 2, "Other", element_type=3)
    for player in (target, other):
        db_session.add(PlayerGameweekStat(player_id=player.id, round=1, minutes=90, total_points=6))
    db_session.commit()

    response = api_client.get(f"/players/{target.id}/similar")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["team_short_name"] == "CHE"


def test_get_similar_players_empty_without_gameweek_history(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    player = _player(db_session, team, 1, "Solo")
    db_session.commit()

    response = api_client.get(f"/players/{player.id}/similar")

    assert response.status_code == 200
    assert response.json() == []


def test_get_player_detail_includes_start_odds_with_the_evidence_behind_them(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session, fpl_id=1)
    opponent = Team(fpl_id=2, name="Chelsea", short_name="CHE")
    db_session.add(opponent)
    db_session.flush()
    player = _player(db_session, team, 1, "Regular")
    for round_number in range(1, 6):
        db_session.add(
            PlayerGameweekStat(
                player_id=player.id,
                round=round_number,
                total_points=5,
                minutes=90,
                starts=1,
                kickoff_time=dt.datetime(2026, 8, 15, tzinfo=dt.UTC)
                + dt.timedelta(days=7 * (round_number - 1)),
            )
        )
    db_session.add(
        Fixture(
            fpl_id=1,
            kickoff_time=dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
            finished=False,
            team_h_id=team.id,
            team_a_id=opponent.id,
            team_h_difficulty=2,
            team_a_difficulty=4,
        )
    )
    db_session.commit()

    body = api_client.get(f"/players/{player.id}").json()

    odds = body["start_odds"]
    assert odds["appearances"] == 5
    assert odds["availability"] == 1.0
    assert odds["start_probability"] == pytest.approx(odds["adjusted_probability"])
    assert odds["start_probability"] > 0.7
    assert odds["evidence_weight"] == pytest.approx(5 / 8)
    assert odds["minutes_load"] == pytest.approx(1.0)
    assert odds["rest_days"] == pytest.approx(7.0)
    assert odds["recent_team_shape"]


def test_start_odds_are_zero_for_a_player_ruled_out_by_the_news(
    db_session: Session, api_client: TestClient
) -> None:
    """The fitness gate stays hard: however reliably a coach picks someone,
    an injured player is not going to start."""
    team = _team(db_session)
    player = _player(db_session, team, 1, "Injured")
    player.status = "i"
    player.chance_of_playing_next_round = 0
    for round_number in range(1, 6):
        db_session.add(
            PlayerGameweekStat(
                player_id=player.id, round=round_number, minutes=90, starts=1, total_points=5
            )
        )
    db_session.commit()

    odds = api_client.get(f"/players/{player.id}").json()["start_odds"]

    assert odds["availability"] == 0.0
    assert odds["adjusted_probability"] > 0.7  # they'd start if fit
    assert odds["start_probability"] == 0.0


def test_start_odds_report_no_evidence_before_a_ball_is_kicked(
    db_session: Session, api_client: TestClient
) -> None:
    """Preseason there is nothing to judge selection on, and `evidence_weight`
    has to say so — the probability is the bare positional prior, not a finding
    about this player."""
    team = _team(db_session)
    player = _player(db_session, team, 1, "Unplayed", element_type=4)
    db_session.commit()

    odds = api_client.get(f"/players/{player.id}").json()["start_odds"]

    assert odds["appearances"] == 0
    assert odds["evidence_weight"] == 0.0
    assert odds["baseline_probability"] == pytest.approx(0.4)  # the forward prior


def _fixture(session: Session, home: Team, away: Team, fpl_id: int, event: int) -> Fixture:
    fixture = Fixture(
        fpl_id=fpl_id,
        event=event,
        team_h_id=home.id,
        team_a_id=away.id,
        team_h_difficulty=3,
        team_a_difficulty=3,
        kickoff_time=dt.datetime(2026, 9, 1, 14, tzinfo=dt.UTC) + dt.timedelta(days=7 * event),
        finished=False,
    )
    session.add(fixture)
    session.flush()
    return fixture


def test_the_player_detail_carries_the_news_and_its_per_gameweek_availability(
    db_session: Session, api_client: TestClient
) -> None:
    """The availability series shown on a profile has to be the one the model
    acted on, and its first entry has to agree with `chance_of_playing` — a UI
    showing two different numbers for the same round is worse than one number."""
    team = _team(db_session)
    other = _team(db_session, fpl_id=2)
    player = _player(db_session, team, 1, "Banned")
    player.status = "s"
    player.news = "Suspended until 30 Aug"
    player.chance_of_playing_next_round = 0
    _player(db_session, other, 2, "Fit")
    _fixture(db_session, team, other, fpl_id=1, event=1)
    _fixture(db_session, team, other, fpl_id=2, event=2)
    db_session.commit()

    body = api_client.get(f"/players/{player.id}").json()

    news = body["news"]
    assert news["category"] == "suspended"
    assert news["headline"] == "Suspended until 30 Aug"
    assert news["return_is_certain"] is True
    assert news["by_event"][0]["availability"] == body["chance_of_playing"] == 0.0


def test_the_news_list_leaves_out_fit_players_and_undated_absences(
    db_session: Session, api_client: TestClient
) -> None:
    team = _team(db_session)
    other = _team(db_session, fpl_id=2)
    fit = _player(db_session, team, 1, "Fit")
    out = _player(db_session, team, 2, "Injured")
    out.status = "i"
    out.news = "Knee injury - Unknown return date"
    out.chance_of_playing_next_round = 0
    _player(db_session, other, 3, "Other")
    for event in (1, 2, 3):
        _fixture(db_session, team, other, fpl_id=event, event=event)
    db_session.commit()

    listed = {row["player_id"] for row in api_client.get("/news").json()}

    assert out.id in listed
    assert fit.id not in listed, "a fit player is not news"
    # An injury with no return date is real news that still carries no *time*
    # information, so it is left out of the actionable list.
    assert api_client.get("/news", params={"only_time_varying": True}).json() == []
