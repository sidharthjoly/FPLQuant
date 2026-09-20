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


def _one_player(session: Session, ep_next: float = 4.0) -> Player:
    team = _team(session, fpl_id=99)
    player = Player(
        fpl_id=99,
        team_id=team.id,
        first_name="Some",
        second_name="Player",
        web_name="Player",
        element_type=3,
        now_cost=60,
        ep_next=ep_next,
        status="a",
    )
    session.add(player)
    session.flush()
    return player


def test_an_unknown_projection_is_refused_rather_than_guessed(db_session: Session) -> None:
    with pytest.raises(ValueError, match="Unknown projection"):
        build_candidates_from_db(db_session, projection="vibes")


def test_the_engine_falls_back_to_form_when_there_is_no_fixture_to_project(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of season, or before a fixture list has been ingested, the engine
    has no gameweek to project. Handing the solver a pool of zeros would turn
    a quiet week into an unsolvable one, so the form projection answers."""
    _one_player(db_session)
    monkeypatch.setattr("fplquant.optimizer.candidates.next_event_points", lambda session: ({}, {}))

    engine = build_candidates_from_db(db_session, projection="engine")
    form = build_candidates_from_db(db_session, projection="form")

    assert engine
    assert {c.player_id: c.predicted_points for c in engine} == {
        c.player_id: c.predicted_points for c in form
    }


def test_only_the_engine_reports_selection_odds(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start_probability` is "will he be named in the XI", which only the
    engine models. None means "not modelled here" and must not be confused
    with a fit player being unlikely to start."""
    _one_player(db_session)
    players = db_session.query(Player).all()
    monkeypatch.setattr(
        "fplquant.optimizer.candidates.next_event_points",
        lambda session: (
            {p.id: 5.0 for p in players},
            {p.id: 0.75 for p in players},
        ),
    )

    engine = build_candidates_from_db(db_session, projection="engine")
    form = build_candidates_from_db(db_session, projection="form")

    assert engine and form
    assert all(c.start_probability == 0.75 for c in engine)
    assert all(c.predicted_points == 5.0 for c in engine)
    assert all(c.start_probability is None for c in form)
