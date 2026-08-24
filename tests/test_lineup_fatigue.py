import datetime as dt

import pytest
from sqlalchemy.orm import Session

from fplquant.lineup.fatigue import compute_fatigue_scores
from tests.lineup_helpers import (
    SEASON_START,
    make_next_fixture,
    make_player,
    make_stat,
    make_team,
)


def _setup(session: Session, *, next_kickoff: dt.datetime):
    home = make_team(session, 1, "ARS")
    away = make_team(session, 2, "CHE")
    make_next_fixture(session, home, away, kickoff=next_kickoff)
    return home, away


def test_normal_weekly_turnaround_is_not_fatiguing(db_session: Session) -> None:
    home, _ = _setup(db_session, next_kickoff=SEASON_START + dt.timedelta(days=7))
    player = make_player(db_session, home, fpl_id=1)
    make_stat(db_session, player, round_number=1, minutes=90, kickoff=SEASON_START)

    score = compute_fatigue_scores(db_session)[0]

    assert score.rest_days == pytest.approx(7.0)
    assert score.fatigue_index == 0.0


def test_midweek_turnaround_after_a_full_workload_is_fatiguing(db_session: Session) -> None:
    home, _ = _setup(db_session, next_kickoff=SEASON_START + dt.timedelta(days=3))
    player = make_player(db_session, home, fpl_id=1)
    make_stat(db_session, player, round_number=1, minutes=90, kickoff=SEASON_START)

    score = compute_fatigue_scores(db_session)[0]

    assert score.rest_days == pytest.approx(3.0)
    assert score.minutes_load == pytest.approx(1.0)
    assert score.fatigue_index == pytest.approx(1.0)


def test_a_benched_player_is_not_tired_however_congested_the_fixture_list(
    db_session: Session,
) -> None:
    """Congestion alone must not read as fatigue — someone who watched the
    midweek game from the bench is the *freshest* player available, and is
    exactly who a rotating manager turns to."""
    home, _ = _setup(db_session, next_kickoff=SEASON_START + dt.timedelta(days=3))
    player = make_player(db_session, home, fpl_id=1)
    make_stat(db_session, player, round_number=1, minutes=0, starts=0, kickoff=SEASON_START)

    score = compute_fatigue_scores(db_session)[0]

    assert score.minutes_load == 0.0
    assert score.fatigue_index == 0.0


def test_rest_is_measured_from_the_last_appearance_not_the_last_fixture(
    db_session: Session,
) -> None:
    home, _ = _setup(db_session, next_kickoff=SEASON_START + dt.timedelta(days=14))
    player = make_player(db_session, home, fpl_id=1)
    make_stat(db_session, player, round_number=1, minutes=90, kickoff=SEASON_START)
    # Played in GW1, then sat out GW2 entirely — so they've had a fortnight off.
    make_stat(
        db_session,
        player,
        round_number=2,
        minutes=0,
        starts=0,
        kickoff=SEASON_START + dt.timedelta(days=7),
    )

    score = compute_fatigue_scores(db_session)[0]

    assert score.rest_days == pytest.approx(14.0)
    assert score.fatigue_index == 0.0


def test_partial_congestion_scales_between_fresh_and_midweek(db_session: Session) -> None:
    home, _ = _setup(db_session, next_kickoff=SEASON_START + dt.timedelta(days=5))
    player = make_player(db_session, home, fpl_id=1)
    make_stat(db_session, player, round_number=1, minutes=90, kickoff=SEASON_START)

    score = compute_fatigue_scores(db_session)[0]

    # 5 days sits halfway between the 7-day fresh mark and the 3-day midweek one.
    assert score.fatigue_index == pytest.approx(0.5)


def test_no_next_fixture_means_no_fatigue_signal(db_session: Session) -> None:
    home = make_team(db_session, 1, "ARS")
    player = make_player(db_session, home, fpl_id=1)
    make_stat(db_session, player, round_number=1, minutes=90, kickoff=SEASON_START)

    score = compute_fatigue_scores(db_session)[0]

    assert score.rest_days is None
    assert score.fatigue_index == 0.0


def test_a_stale_fixture_row_reads_as_unknown_rest_not_as_zero_days(db_session: Session) -> None:
    """If a played fixture hasn't been flagged finished yet, the "next" fixture
    can pre-date the player's last appearance. That's a data disagreement, not a
    zero-day turnaround, and must not be charged as maximum fatigue."""
    home, _ = _setup(db_session, next_kickoff=SEASON_START - dt.timedelta(days=3))
    player = make_player(db_session, home, fpl_id=1)
    make_stat(db_session, player, round_number=1, minutes=90, kickoff=SEASON_START)

    score = compute_fatigue_scores(db_session)[0]

    assert score.rest_days is None
    assert score.fatigue_index == 0.0
