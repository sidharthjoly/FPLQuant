import datetime as dt

import pytest
from sqlalchemy.orm import Session

from fplquant.form.fixtures import compute_fixture_adjusted_scores
from fplquant.lineup.starts import (
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    combined_start_probability,
    compute_start_probabilities,
    did_start,
)
from fplquant.models.orm import PlayerGameweekStat
from tests.lineup_helpers import (
    DEF,
    SEASON_START,
    make_next_fixture,
    make_player,
    make_stat,
    make_team,
)


def _fixture_in(session: Session, days: int):
    home = make_team(session, 1, "ARS")
    away = make_team(session, 2, "CHE")
    make_next_fixture(session, home, away, kickoff=SEASON_START + dt.timedelta(days=days))
    return home


def test_did_start_uses_the_explicit_flag_when_present() -> None:
    assert did_start(PlayerGameweekStat(minutes=20, starts=1)) is True
    assert did_start(PlayerGameweekStat(minutes=80, starts=0)) is False


def test_did_start_falls_back_to_minutes_on_rows_ingested_before_the_column_existed() -> None:
    """A database that hasn't been re-ingested has `starts` NULL on every row.
    Reading those as "did not start" would collapse every start probability to
    the floor, so fall back to a minutes threshold instead."""
    assert did_start(PlayerGameweekStat(minutes=90, starts=None)) is True
    assert did_start(PlayerGameweekStat(minutes=0, starts=None)) is False
    assert did_start(PlayerGameweekStat(minutes=12, starts=None)) is False


def test_a_player_with_no_history_falls_back_to_the_positional_prior(db_session: Session) -> None:
    home = _fixture_in(db_session, 7)
    make_player(db_session, home, fpl_id=1)

    probability = compute_start_probabilities(db_session)[0]

    assert probability.appearances == 0
    assert probability.baseline_probability == pytest.approx(0.45)
    assert probability.lineup_multiplier == 1.0


def test_a_regular_starter_outranks_a_fringe_squad_player(db_session: Session) -> None:
    home = _fixture_in(db_session, 7)
    regular = make_player(db_session, home, fpl_id=1)
    fringe = make_player(db_session, home, fpl_id=2)
    for round_number in range(1, 11):
        kickoff = SEASON_START - dt.timedelta(days=7 * (11 - round_number))
        make_stat(db_session, regular, round_number=round_number, starts=1, kickoff=kickoff)
        make_stat(
            db_session, fringe, round_number=round_number, minutes=0, starts=0, kickoff=kickoff
        )

    by_id = {p.player_id: p for p in compute_start_probabilities(db_session)}

    assert by_id[regular.id].baseline_probability > 0.85
    assert by_id[fringe.id].baseline_probability < 0.15


def test_the_multiplier_is_a_no_op_on_a_normal_week(db_session: Session) -> None:
    """The key property: with a full week's rest and a settled shape, the model
    has nothing to add beyond what the player's points history already says, so
    it must not move the estimate at all."""
    home = _fixture_in(db_session, 7)
    player = make_player(db_session, home, fpl_id=1)
    for round_number in range(1, 11):
        make_stat(
            db_session,
            player,
            round_number=round_number,
            minutes=90,
            starts=1,
            kickoff=SEASON_START - dt.timedelta(days=7 * (11 - round_number)),
        )

    probability = compute_start_probabilities(db_session)[0]

    assert probability.fatigue_index == 0.0
    assert probability.lineup_multiplier == pytest.approx(1.0)


def test_a_congested_fixture_penalises_a_heavily_played_starter(db_session: Session) -> None:
    home = _fixture_in(db_session, 3)
    player = make_player(db_session, home, fpl_id=1)
    for round_number in range(1, 11):
        make_stat(
            db_session,
            player,
            round_number=round_number,
            minutes=90,
            starts=1,
            kickoff=SEASON_START - dt.timedelta(days=7 * (10 - round_number)),
        )

    probability = compute_start_probabilities(db_session)[0]

    assert probability.fatigue_index == pytest.approx(1.0)
    assert probability.lineup_multiplier < 1.0
    assert probability.adjusted_probability < probability.baseline_probability


def test_the_multiplier_stays_within_its_clamp(db_session: Session) -> None:
    home = _fixture_in(db_session, 1)
    player = make_player(db_session, home, fpl_id=1)
    for round_number in range(1, 11):
        make_stat(
            db_session,
            player,
            round_number=round_number,
            minutes=90,
            starts=1,
            kickoff=SEASON_START - dt.timedelta(days=7 * (10 - round_number)),
        )

    probability = compute_start_probabilities(db_session)[0]

    assert MIN_MULTIPLIER <= probability.lineup_multiplier <= MAX_MULTIPLIER


def test_the_news_gate_stays_hard_and_is_not_softened_by_the_lineup_signal(
    db_session: Session,
) -> None:
    """An injured player must stay at zero expected points. If the rotation
    signal were allowed to average a flagged player back toward "he usually
    starts", the optimizer would happily buy injured players."""
    home = _fixture_in(db_session, 7)
    injured = make_player(db_session, home, fpl_id=1, status="i")
    for round_number in range(1, 11):
        make_stat(
            db_session,
            injured,
            round_number=round_number,
            minutes=90,
            starts=1,
            kickoff=SEASON_START - dt.timedelta(days=7 * (11 - round_number)),
        )

    score = next(
        s for s in compute_fixture_adjusted_scores(db_session) if s.player_id == injured.id
    )

    assert score.chance_of_playing == 0.0
    assert score.adjusted_points == 0.0


def test_the_multiplier_reaches_expected_points(db_session: Session) -> None:
    home = _fixture_in(db_session, 3)
    tired = make_player(db_session, home, fpl_id=1)
    for round_number in range(1, 11):
        make_stat(
            db_session,
            tired,
            round_number=round_number,
            minutes=90,
            starts=1,
            total_points=5,
            kickoff=SEASON_START - dt.timedelta(days=7 * (10 - round_number)),
        )

    score = next(s for s in compute_fixture_adjusted_scores(db_session) if s.player_id == tired.id)

    assert score.lineup_multiplier < 1.0
    assert score.adjusted_points == pytest.approx(
        score.base_points * score.fixture_multiplier * score.lineup_multiplier
    )


def test_a_recent_shape_change_is_discounted_until_there_is_evidence_for_it(
    db_session: Session,
) -> None:
    """A club naming three defenders for two weeks after a season of back fours
    is more likely to be personnel than a system change, so the penalty to their
    defenders is heavily discounted rather than applied at face value."""
    home = _fixture_in(db_session, 7)
    away = make_team(db_session, 3, "LIV")
    defenders = [make_player(db_session, home, fpl_id=i, element_type=DEF) for i in range(10, 15)]
    for round_number in range(1, 11):
        kickoff = SEASON_START - dt.timedelta(days=7 * (11 - round_number))
        # A back four for eight weeks, then a back three for the last two.
        starting = defenders[:4] if round_number <= 8 else defenders[:3]
        for defender in defenders:
            started = defender in starting
            make_stat(
                db_session,
                defender,
                round_number=round_number,
                minutes=90 if started else 0,
                starts=1 if started else 0,
                kickoff=kickoff,
            )
    make_next_fixture(db_session, away, home, kickoff=SEASON_START, fpl_id=901)

    by_id = {p.player_id: p for p in compute_start_probabilities(db_session)}
    dropped = by_id[defenders[3].id]

    # The shape really has tightened, so the factor is below 1.0 ...
    assert dropped.formation_factor < 1.0
    # ... but nowhere near the raw 3/4 the last two weeks taken alone would imply.
    assert dropped.formation_factor > 0.9


def test_combined_start_probability_is_the_news_alone_without_a_start_estimate() -> None:
    assert combined_start_probability(None, 0.25) == pytest.approx(0.25)


def test_combined_start_probability_gates_selection_odds_on_the_fitness_news(
    db_session: Session,
) -> None:
    """A nailed-on starter who is ruled out is not going to start, however
    reliably their coach picks them when they're fit."""
    home = _fixture_in(db_session, 7)
    player = make_player(db_session, home, fpl_id=1)
    for round_number in range(1, 11):
        make_stat(db_session, player, round_number=round_number)

    start = compute_start_probabilities(db_session)[0]

    assert start.adjusted_probability > 0.8
    assert combined_start_probability(start, 0.0) == 0.0
    assert combined_start_probability(start, 0.5) == pytest.approx(start.adjusted_probability * 0.5)
    assert combined_start_probability(start, 1.0) == pytest.approx(start.adjusted_probability)


def test_evidence_weight_reports_how_much_of_the_baseline_is_the_players_own_record(
    db_session: Session,
) -> None:
    home = _fixture_in(db_session, 7)
    unplayed = make_player(db_session, home, fpl_id=1)
    established = make_player(db_session, home, fpl_id=2)
    for round_number in range(1, 10):
        make_stat(db_session, established, round_number=round_number)

    by_id = {p.player_id: p for p in compute_start_probabilities(db_session)}

    # With nothing on record the baseline is entirely the positional prior ...
    assert by_id[unplayed.id].evidence_weight == 0.0
    # ... and after nine appearances it is mostly the player's own start rate.
    assert by_id[established.id].evidence_weight == pytest.approx(9 / 12)


def test_start_probability_carries_the_evidence_behind_it(db_session: Session) -> None:
    """The breakdown the explorer shows: recent workload and the shape the club
    has actually been naming, not just the headline number."""
    # Five weekly rounds played, then the next kickoff a normal week after the
    # last of them.
    home = _fixture_in(db_session, 35)
    defenders = [make_player(db_session, home, fpl_id=i, element_type=DEF) for i in range(1, 5)]
    for round_number in range(1, 6):
        for defender in defenders:
            make_stat(db_session, defender, round_number=round_number, minutes=90)

    start = compute_start_probabilities(db_session)[0]

    assert start.minutes_load == pytest.approx(1.0)
    assert start.rest_days == pytest.approx(7.0)
    # A back four named every week, so both the season-long shape and the
    # recent one report four defenders.
    assert start.team_shape.startswith("4-")
    assert start.recent_team_shape.startswith("4-")
