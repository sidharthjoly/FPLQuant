"""Scoring a squad against what its players went on to do.

The arithmetic here is deliberately free of the solver and the database, so it
is tested that way: hand it a squad and a set of results and check what comes
back.
"""

from fplquant.backtest.lineups import (
    CALIBRATION_BANDS,
    calibration,
    score_lineup,
)
from fplquant.optimizer.starting_xi import select_starting_xi
from fplquant.optimizer.types import (
    DEFENDER,
    FORWARD,
    GOALKEEPER,
    MIDFIELDER,
    OptimizedSquad,
    PlayerCandidate,
)

# 2 GKP / 5 DEF / 5 MID / 3 FWD, projections descending within each position.
_COMPOSITION = [GOALKEEPER] * 2 + [DEFENDER] * 5 + [MIDFIELDER] * 5 + [FORWARD] * 3


def _squad(projections: list[float] | None = None) -> list[PlayerCandidate]:
    projected = projections or [float(15 - i) for i in range(15)]
    return [
        PlayerCandidate(
            player_id=i,
            web_name=f"P{i}",
            team_id=i % 5,
            team_short_name="ARS",
            element_type=position,
            now_cost=50,
            predicted_points=projected[i],
        )
        for i, position in enumerate(_COMPOSITION)
    ]


def _optimized(players: list[PlayerCandidate]) -> OptimizedSquad:
    return OptimizedSquad(
        players=players,
        total_cost=sum(p.now_cost for p in players),
        total_predicted_points=sum(p.predicted_points for p in players),
    )


def test_the_captain_is_counted_twice_and_reported_once() -> None:
    """`total` is what a manager would have scored; `starting_points` is the
    XI with the armband counted once. Conflating them double-counts or loses
    a captain depending on which way you get it wrong."""
    squad = _squad()
    xi = select_starting_xi(squad)
    points = {p.player_id: 2.0 for p in squad}
    points[xi.captain.player_id] = 10.0

    score = score_lineup("engine", 4, _optimized(squad), xi, points)

    assert score.starting_points == 2.0 * 10 + 10.0
    assert score.captain_points == 10.0
    assert score.total == score.starting_points + 10.0


def test_captain_regret_is_what_the_armband_left_behind() -> None:
    squad = _squad()
    xi = select_starting_xi(squad)
    points = {p.player_id: 1.0 for p in squad}
    points[xi.captain.player_id] = 3.0
    best = next(p for p in xi.starters if p.player_id != xi.captain.player_id)
    points[best.player_id] = 12.0

    score = score_lineup("engine", 4, _optimized(squad), xi, points)

    assert score.captain_regret == 9.0


def test_a_perfect_week_captures_all_of_its_ceiling() -> None:
    """If the projection ordered the squad exactly as the results did, there
    was nothing left on the table and `capture` has to say so."""
    squad = _squad()
    xi = select_starting_xi(squad)
    points = {p.player_id: p.predicted_points for p in squad}

    score = score_lineup("engine", 4, _optimized(squad), xi, points)

    assert score.capture == 1.0
    assert score.total == score.best_possible


def test_the_ceiling_uses_the_points_the_bench_actually_scored() -> None:
    """The ceiling is the best legal XI from the same fifteen, so a bench
    player who hauled has to be able to reach it."""
    squad = _squad()
    xi = select_starting_xi(squad)
    points = {p.player_id: 1.0 for p in squad}
    benched = xi.bench[-1]
    points[benched.player_id] = 20.0

    score = score_lineup("engine", 4, _optimized(squad), xi, points)

    assert score.bench_points >= 20.0
    # 10 starters on 1, the hauler starting and captained.
    assert score.best_possible == 10 * 1.0 + 20.0 + 20.0
    assert score.capture < 1.0


def test_blanks_count_starters_who_returned_nothing() -> None:
    squad = _squad()
    xi = select_starting_xi(squad)
    points = {p.player_id: 0.0 for p in squad}
    points[xi.starters[0].player_id] = 6.0

    score = score_lineup("engine", 4, _optimized(squad), xi, points)

    assert score.blanks == 10


def test_calibration_ranks_inside_each_round_before_pooling() -> None:
    """Pooling first and ranking after would make "the top five" mean the five
    highest projections of the season — a handful of players in a handful of
    fixtures — rather than the five the model liked most each week."""
    quiet_week = [(3.0, 3.0)] * 10
    big_week = [(9.0, 30.0)] * 10

    bands = calibration([quiet_week, big_week])
    top = next(band for band in bands if (band.low, band.high) == (1, 5))

    # Five from each week, not ten from the big one.
    assert top.n == 10
    assert top.predicted == 6.0
    assert top.actual == 16.5


def test_calibration_skips_bands_no_round_reaches() -> None:
    bands = calibration([[(5.0, 4.0)] * 3])
    assert [(band.low, band.high) for band in bands] == [CALIBRATION_BANDS[0]]
    assert bands[0].n == 3


def test_a_week_where_nobody_scored_does_not_divide_by_zero() -> None:
    """A blanked squad has a ceiling of nought, and `capture` is then asking
    what share of nothing was realised. It has to answer rather than raise."""
    squad = _squad()
    xi = select_starting_xi(squad)
    points = {p.player_id: 0.0 for p in squad}

    score = score_lineup("engine", 4, _optimized(squad), xi, points)

    assert score.best_possible == 0.0
    assert score.capture == 0.0
