import pytest
from sqlalchemy.orm import Session

from fplquant.models.orm import Player, PlayerGameweekStat, Team
from fplquant.optimizer.candidates import (
    build_candidates_from_db,
    build_risk_adjusted_candidates_from_db,
)


def _team(session: Session, fpl_id: int = 1) -> Team:
    team = Team(fpl_id=fpl_id, name="Arsenal", short_name="ARS")
    session.add(team)
    session.flush()
    return team


def test_uses_ep_next_when_no_gameweek_history(db_session: Session) -> None:
    team = _team(db_session)
    player = Player(
        fpl_id=1,
        team_id=team.id,
        first_name="No",
        second_name="History",
        web_name="NoHistory",
        element_type=4,
        now_cost=70,
        ep_next=5.5,
        status="a",
    )
    db_session.add(player)
    db_session.flush()

    candidates = build_candidates_from_db(db_session)

    assert len(candidates) == 1
    assert candidates[0].predicted_points == 5.5


def test_prefers_points_form_when_history_exists(db_session: Session) -> None:
    team = _team(db_session)
    player = Player(
        fpl_id=1,
        team_id=team.id,
        first_name="Has",
        second_name="History",
        web_name="HasHistory",
        element_type=4,
        now_cost=70,
        ep_next=1.0,  # deliberately low, to prove form data wins when present
        status="a",
    )
    db_session.add(player)
    db_session.flush()
    for round_number, pts in enumerate([8, 8, 8], start=1):
        db_session.add(
            PlayerGameweekStat(
                player_id=player.id, round=round_number, minutes=90, total_points=pts
            )
        )
    db_session.flush()

    candidates = build_candidates_from_db(db_session)

    # Form pulls the estimate well above the deliberately low ep_next, but is
    # shrunk toward it in proportion to the three appearances behind it:
    # 8.0 * 3/9 + 1.0 * 6/9. See form.scoring.predicted_points_by_player.
    assert candidates[0].predicted_points == pytest.approx(3.0 + 1.0 / 3.0)


def test_excludes_unavailable_players_by_default(db_session: Session) -> None:
    team = _team(db_session)
    db_session.add(
        Player(
            fpl_id=1,
            team_id=team.id,
            first_name="Gone",
            second_name="Away",
            web_name="Gone",
            element_type=4,
            now_cost=45,
            status="u",
        )
    )
    db_session.flush()

    candidates = build_candidates_from_db(db_session)

    assert candidates == []


def test_includes_unavailable_players_when_flag_disabled(db_session: Session) -> None:
    team = _team(db_session)
    db_session.add(
        Player(
            fpl_id=1,
            team_id=team.id,
            first_name="Gone",
            second_name="Away",
            web_name="Gone",
            element_type=4,
            now_cost=45,
            status="u",
        )
    )
    db_session.flush()

    candidates = build_candidates_from_db(db_session, exclude_unavailable=False)

    assert len(candidates) == 1


def test_risk_adjusted_candidates_discount_injured_players(db_session: Session) -> None:
    team = _team(db_session)
    db_session.add_all(
        [
            Player(
                fpl_id=1,
                team_id=team.id,
                first_name="Injured",
                second_name="Injured",
                web_name="Injured",
                element_type=4,
                now_cost=70,
                ep_next=6.0,
                status="i",
            ),
            Player(
                fpl_id=2,
                team_id=team.id,
                first_name="Fit",
                second_name="Fit",
                web_name="Fit",
                element_type=4,
                now_cost=70,
                ep_next=6.0,
                status="a",
            ),
        ]
    )
    db_session.flush()

    candidates = {c.web_name: c for c in build_risk_adjusted_candidates_from_db(db_session)}

    assert candidates["Injured"].predicted_points < candidates["Fit"].predicted_points


def test_risk_adjusted_candidates_exclude_unavailable_by_default(db_session: Session) -> None:
    team = _team(db_session)
    db_session.add(
        Player(
            fpl_id=1,
            team_id=team.id,
            first_name="Gone",
            second_name="Away",
            web_name="Gone",
            element_type=4,
            now_cost=45,
            status="u",
        )
    )
    db_session.flush()

    candidates = build_risk_adjusted_candidates_from_db(db_session)

    assert candidates == []
