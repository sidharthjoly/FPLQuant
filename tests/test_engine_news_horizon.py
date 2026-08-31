"""Availability varying across the horizon, and what it does to a projection.

Before this existed, `project_horizon` computed usage once and handed the same
`p_start` to a match in November as to one on Saturday. A player serving a ban
that expired on Tuesday was therefore worth exactly zero points for the next
two months, which is the single most consequential thing a transfer planner can
be wrong about: it says sell, unconditionally, a player who is available again
next week.
"""

import datetime as dt
from typing import Any

import pytest
from sqlalchemy.orm import Session

import fplquant.engine.horizon as horizon_module
from fplquant.backtest.hydrate import hydrate
from fplquant.engine.horizon import project_horizon
from fplquant.models.orm import HistoricalPlayerGameweek, Player, Team
from fplquant.optimizer.candidates import build_horizon_candidates_from_db
from tests.engine_helpers import make_fixture, make_league

AS_OF = dt.datetime(2026, 8, 31, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def league(db_session: Session) -> list[Team]:
    """Six clubs playing weekly rounds, the first of them tomorrow.

    Six rather than four so a legal fifteen exists at three players per club,
    which the planner test below needs and the others don't mind.
    """
    teams = make_league(db_session, teams=6)
    for event in range(1, 6):
        for index in range(0, len(teams) - 1, 2):
            make_fixture(
                db_session,
                teams[index],
                teams[index + 1],
                fpl_id=event * 10 + index,
                event=event,
                kickoff=AS_OF + dt.timedelta(days=1 + 7 * (event - 1)),
            )
    return teams


def _freeze_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the horizon's idea of today, which is otherwise the wall clock.

    Bound on `availability_by_event`'s own keyword rather than by patching
    `datetime`: `import datetime as dt` binds the shared module object, so
    replacing `dt.datetime` there would swap the clock for every module in the
    process and quietly change any `default=` timestamp a test happens to write.
    """
    real = horizon_module.availability_by_event
    monkeypatch.setattr(
        horizon_module,
        "availability_by_event",
        lambda session, events, **kwargs: real(session, events, as_of=AS_OF),
    )


def test_a_ban_that_expires_inside_the_horizon_stops_writing_the_player_off(
    db_session: Session, league: list[Team], monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze_today(monkeypatch)
    player = db_session.query(Player).filter(Player.team_id == league[0].id).first()
    assert player is not None
    player.status = "s"
    player.news = "Suspended until 10 Sep"
    player.chance_of_playing_next_round = 0
    db_session.flush()

    projection = next(p for p in project_horizon(db_session, horizon=5) if p.player_id == player.id)
    by_event = projection.points_by_event

    assert by_event[1] == 0.0, "still banned"
    assert by_event[2] == 0.0
    assert by_event[3] > 0.0, "the ban has expired by round three"
    assert projection.total_points > 0.0


def test_a_returning_player_takes_goal_share_back_off_his_teammates(
    db_session: Session, league: list[Team], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why availability is threaded into `compute_player_usage` rather than
    multiplied onto a finished projection. Shares are normalised within a club,
    so a striker coming back has to take goals *off* whoever absorbed them —
    scaling one side of that trade after the fact would count them twice."""
    _freeze_today(monkeypatch)
    squad = db_session.query(Player).filter(Player.team_id == league[0].id).all()
    banned = max(squad, key=lambda p: p.now_cost)
    banned.status = "s"
    banned.news = "Suspended until 10 Sep"
    banned.chance_of_playing_next_round = 0
    db_session.flush()

    projections = {p.player_id: p for p in project_horizon(db_session, horizon=5)}
    teammate = next(p for p in squad if p.element_type == banned.element_type and p.id != banned.id)
    covering = projections[teammate.id].points_by_event

    assert projections[banned.id].points_by_event[3] > 0.0
    assert covering[1] > covering[3], "he was covering while the first choice was out"

    club = [p for p in squad if p.element_type == banned.element_type]
    total_early = sum(projections[p.id].points_by_event[1] for p in club)
    total_late = sum(projections[p.id].points_by_event[3] for p in club)
    assert total_late == pytest.approx(total_early, rel=0.35), (
        "the position group's total should be roughly conserved — the goals moved "
        "between teammates rather than appearing from nowhere"
    )


def test_a_league_with_no_news_costs_exactly_one_usage_computation(
    db_session: Session, league: list[Team], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inertness and the cost guard are the same property. When nothing in the
    pool has a date attached, every event shares one availability vector, so
    the horizon collapses to a single computation and the projection is
    bit-identical to what it was before this layer existed."""
    _freeze_today(monkeypatch)
    calls: list[Any] = []
    original = horizon_module.compute_player_usage

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("availability"))
        return original(*args, **kwargs)

    monkeypatch.setattr(horizon_module, "compute_player_usage", counted)
    projections = project_horizon(db_session, horizon=5)

    assert len(calls) == 1, "one vector, one computation"
    assert projections
    for projection in projections:
        assert projection.usage.p_start > 0.0


def test_the_backtest_is_untouched_because_a_replay_has_no_news(
    db_session: Session,
) -> None:
    """`hydrate` builds a fresh in-memory season from an archive that carries no
    injury news, so `Player.news` is empty for everyone and this layer says
    nothing. That is the property that keeps a replay of gameweek five from
    being gated on today's fitness — the leak `PlayerSnapshot` exists to
    prevent, one level down."""
    rows = [
        HistoricalPlayerGameweek(
            season="2024-25",
            element=element,
            round=round_number,
            fixture=100 + round_number,
            name=f"Player {element}",
            position="MID",
            team="Alpha" if element % 2 else "Beta",
            opponent_team=2 if element % 2 else 1,
            was_home=bool(element % 2),
            kickoff_time=dt.datetime(2024, 8, 17, 14, tzinfo=dt.UTC),
            minutes=90,
            starts=1,
            total_points=5,
            value=60,
            team_h_score=1,
            team_a_score=1,
        )
        for element in (1, 2)
        for round_number in (1, 2)
    ]
    session, _ = hydrate(rows, up_to_round=2)

    assert all(not (p.news or "") for p in session.query(Player).all())
    projections = project_horizon(session, horizon=1, use_minutes_model=False)
    assert projections
    for projection in projections:
        assert projection.usage.p_start > 0.0, "nobody is gated out by news that isn't there"


def test_a_dated_absence_survives_the_planner_pool_trim_where_an_open_ended_one_does_not(
    db_session: Session, league: list[Team], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the multi-gameweek planner is actually handed.

    Both versions of this player are ruled out of the next round and FPL puts
    both at 0% for it; the only difference is whether the news carries a date.
    `build_horizon_candidates_from_db` ranks the pool on `discounted_points` and
    trims it, so a player worth zero across the whole horizon is not merely
    ranked last — he is the one a planner can never choose. Carrying a date is
    what puts him back in front of it.

    The *decision* the planner then makes is its own business and depends on
    what else the money can buy; the single-gameweek transfer planner is
    untouched by any of this, and should be — he really is out this week.
    """
    _freeze_today(monkeypatch)
    premium = max(
        db_session.query(Player).filter(Player.team_id == league[0].id).all(),
        key=lambda p: p.now_cost,
    )

    def horizon_value(news: str) -> float:
        premium.news = news
        premium.status = "s" if news.startswith("Suspended") else "i"
        premium.chance_of_playing_next_round = 0
        db_session.flush()
        candidates, _ = build_horizon_candidates_from_db(
            db_session, horizon=3, always_include={premium.id}
        )
        return next(
            c.candidate.predicted_points for c in candidates if c.candidate.player_id == premium.id
        )

    assert horizon_value("Knee injury - Unknown return date") == 0.0
    assert horizon_value("Suspended until 10 Sep") > 0.0
