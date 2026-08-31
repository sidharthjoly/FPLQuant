import math
from dataclasses import replace

import pytest

from fplquant.engine import scoring
from fplquant.engine.scoring import (
    CLEAN_SHEET_POINTS,
    GOAL_POINTS,
    PlayerFixtureInputs,
    clean_sheet_probability,
    expected_points,
    expected_step_count,
    poisson_pmf,
)
from fplquant.engine.usage import POSITION_DEFENSIVE_ACTIONS_PER_90
from fplquant.optimizer.types import DEFENDER, FORWARD, GOALKEEPER, MIDFIELDER


def _inputs(**overrides: float | int) -> PlayerFixtureInputs:
    defaults: dict[str, float | int] = {
        "element_type": MIDFIELDER,
        "p_start": 0.9,
        "p_bench_appearance": 0.2,
        "expected_minutes": 80.0,
        "expected_goals": 0.3,
        "expected_assists": 0.2,
        "lambda_conceded": 1.2,
        "expected_bonus": 0.4,
    }
    defaults.update(overrides)
    return PlayerFixtureInputs(**defaults)  # type: ignore[arg-type]


def test_poisson_pmf_sums_to_one() -> None:
    assert sum(poisson_pmf(1.7, k) for k in range(40)) == pytest.approx(1.0, abs=1e-9)


def test_clean_sheet_probability_falls_as_the_opponent_gets_better() -> None:
    assert clean_sheet_probability(0.8) > clean_sheet_probability(2.0)
    assert clean_sheet_probability(1.0) == pytest.approx(math.exp(-1.0))


def test_expected_step_count_is_not_the_step_of_the_expectation() -> None:
    """FPL charges a point per *two* goals conceded, and E[floor(K/2)] is not
    floor(E[K]/2). Taking the naive version would tell every defender facing a
    1.5-goal attack that they lose nothing, when they lose about half a point —
    a small error applied identically to every defender in the pool."""
    naive = math.floor(1.5 / 2)
    assert naive == 0
    assert expected_step_count(1.5, 2) == pytest.approx(0.512, abs=0.01)


def test_a_forward_gets_nothing_for_a_clean_sheet() -> None:
    forward = expected_points(_inputs(element_type=FORWARD))
    midfielder = expected_points(_inputs(element_type=MIDFIELDER))
    assert forward.clean_sheet == 0.0
    assert midfielder.clean_sheet > 0.0
    assert CLEAN_SHEET_POINTS[FORWARD] == 0


def test_a_defender_is_worth_more_against_a_toothless_attack() -> None:
    easy = expected_points(_inputs(element_type=DEFENDER, lambda_conceded=0.7))
    hard = expected_points(_inputs(element_type=DEFENDER, lambda_conceded=2.4))
    assert easy.total > hard.total
    assert easy.clean_sheet > hard.clean_sheet
    assert easy.goals_conceded > hard.goals_conceded  # both negative; the easy tie loses less


def test_only_goalkeepers_earn_save_points() -> None:
    keeper = expected_points(_inputs(element_type=GOALKEEPER, expected_goals=0.0))
    defender = expected_points(_inputs(element_type=DEFENDER, expected_goals=0.0))
    assert keeper.saves > 0.0
    assert defender.saves == 0.0


def test_goals_are_scored_at_the_rate_for_the_position() -> None:
    """The same expected goals are worth more to a defender than to a forward,
    which is the whole reason the scoring table is a table."""
    defender = expected_points(_inputs(element_type=DEFENDER, expected_goals=0.5))
    forward = expected_points(_inputs(element_type=FORWARD, expected_goals=0.5))
    assert defender.goals == pytest.approx(0.5 * GOAL_POINTS[DEFENDER])
    assert forward.goals == pytest.approx(0.5 * GOAL_POINTS[FORWARD])
    assert defender.goals > forward.goals


def test_a_player_who_never_plays_scores_almost_nothing() -> None:
    benched = expected_points(
        _inputs(
            p_start=0.0,
            p_bench_appearance=0.0,
            expected_minutes=0.0,
            expected_goals=0.0,
            expected_assists=0.0,
        )
    )
    assert benched.total == pytest.approx(0.0, abs=1e-9)


def test_the_breakdown_adds_up_to_the_total() -> None:
    """The split is the interpretable part of this model, so it has to be the
    same arithmetic as the number the optimizer consumes, not a parallel one."""
    breakdown = expected_points(_inputs(element_type=GOALKEEPER))
    parts = (
        breakdown.appearance
        + breakdown.goals
        + breakdown.assists
        + breakdown.clean_sheet
        + breakdown.goals_conceded
        + breakdown.saves
        + breakdown.bonus
        + breakdown.cards
    )
    assert parts == pytest.approx(breakdown.total)


def test_expected_step_count_rejects_a_zero_divisor() -> None:
    with pytest.raises(ValueError):
        expected_step_count(1.0, 0)


def _defender(actions_per_90: float, p_start: float = 0.95) -> scoring.PlayerFixtureInputs:
    return scoring.PlayerFixtureInputs(
        element_type=DEFENDER,
        p_start=p_start,
        p_bench_appearance=0.1,
        expected_minutes=84.0 * p_start,
        expected_goals=0.05,
        expected_assists=0.05,
        lambda_conceded=1.3,
        expected_bonus=0.2,
        defensive_actions_per_90=actions_per_90,
    )


def test_defensive_contribution_is_a_threshold_not_a_rate() -> None:
    """Evaluating the rule at a player's average would score 9.5 actions as
    nothing and 10.5 as the full two points. Both cross the line about half the
    time, and the model has to say so."""
    just_under = scoring.expected_points(_defender(9.5)).defensive_contribution
    just_over = scoring.expected_points(_defender(10.5)).defensive_contribution

    assert 0.5 < just_under < 1.2
    assert 0.8 < just_over < 1.6
    assert just_over > just_under
    # Nowhere near the 0 / 2 step that a mean-based rule would produce.
    assert just_over - just_under < 0.5


def test_a_defender_who_never_tackles_earns_no_defensive_points() -> None:
    assert scoring.expected_points(_defender(0.0)).defensive_contribution == 0.0


def test_goalkeepers_are_not_eligible_for_defensive_contribution() -> None:
    keeper = replace(_defender(15.0), element_type=GOALKEEPER)
    assert scoring.expected_points(keeper).defensive_contribution == 0.0


def test_the_defensive_term_matches_the_rate_measured_in_2025_26() -> None:
    """Calibration, against the real thing rather than against itself.

    Averaged over every 2025-26 defender appearance in the archive, Defensive
    Contribution was worth 0.416 points an appearance. A defender on the
    positional prior, playing a typical full match, has to land near that or the
    term is decorative — and it has to do so using the constant the engine
    actually ships, not one written into the test.
    """
    rate = POSITION_DEFENSIVE_ACTIONS_PER_90[DEFENDER]
    typical = scoring.expected_points(_defender(rate, p_start=1.0))

    assert 0.35 < typical.defensive_contribution < 0.60


def test_defensive_contribution_is_included_in_the_total() -> None:
    breakdown = scoring.expected_points(_defender(12.0))
    parts = (
        breakdown.appearance
        + breakdown.goals
        + breakdown.assists
        + breakdown.clean_sheet
        + breakdown.goals_conceded
        + breakdown.saves
        + breakdown.bonus
        + breakdown.cards
        + breakdown.defensive_contribution
    )
    assert breakdown.defensive_contribution > 0
    assert breakdown.total == pytest.approx(parts)
