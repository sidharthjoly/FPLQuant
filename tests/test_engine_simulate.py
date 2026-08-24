import numpy as np
import pytest
from sqlalchemy.orm import Session

from fplquant.engine.horizon import project_horizon
from fplquant.engine.simulate import simulate_event, summarize_player, summarize_squad
from tests.engine_helpers import DEF, make_fixture, make_league


def _projections(db_session: Session) -> tuple[list, int]:
    teams = make_league(db_session, teams=4)
    make_fixture(db_session, teams[0], teams[1], fpl_id=1, event=1)
    make_fixture(db_session, teams[2], teams[3], fpl_id=2, event=1)
    return project_horizon(db_session, horizon=1), 1


def test_the_simulation_reproduces_the_analytic_model(db_session: Session) -> None:
    """The strongest check available on either half of the engine: the closed
    form in `scoring.py` and the sampler in `simulate.py` are independent
    implementations of the same model, so their means have to agree. They
    disagreed by nearly 20% until the sampler stopped applying expected minutes
    twice — a bias no single-implementation test would have caught."""
    projections, event = _projections(db_session)

    samples = simulate_event(projections, event, simulations=20000, seed=11)

    errors = []
    for projection in projections:
        analytic = projection.events[0].points
        if analytic < 1.0:
            continue  # fringe players; the relative error on a near-zero mean is noise
        errors.append(float(np.mean(samples[projection.player_id])) - analytic)

    assert errors, "no players were worth comparing"
    assert abs(float(np.mean(errors))) < 0.1
    assert max(abs(error) for error in errors) < 0.35


def test_the_same_seed_gives_the_same_gameweek(db_session: Session) -> None:
    projections, event = _projections(db_session)

    first = simulate_event(projections, event, simulations=200, seed=5)
    again = simulate_event(projections, event, simulations=200, seed=5)
    different = simulate_event(projections, event, simulations=200, seed=6)

    player_id = projections[0].player_id
    assert np.array_equal(first[player_id], again[player_id])
    assert not np.array_equal(first[player_id], different[player_id])


def test_teammates_returns_arrive_together(db_session: Session) -> None:
    """Three defenders from one club are not three independent bets on a clean
    sheet, they are one bet held three times. The correlation is structural
    here — they read the same goals-conceded draw — rather than estimated from
    past points, so it is present from the first simulation."""
    projections, event = _projections(db_session)
    samples = simulate_event(projections, event, simulations=4000, seed=3)

    club = projections[0].team_id
    defenders = [
        p.player_id
        for p in projections
        if p.team_id == club and p.element_type == DEF and p.usage.p_start > 0.5
    ][:2]
    opponents = [p.player_id for p in projections if p.team_id != club][:1]

    teammate_correlation = float(np.corrcoef(samples[defenders[0]], samples[defenders[1]])[0, 1])
    stranger_correlation = float(np.corrcoef(samples[defenders[0]], samples[opponents[0]])[0, 1])
    assert teammate_correlation > 0.1
    assert teammate_correlation > stranger_correlation


def test_a_squad_is_riskier_than_the_sum_of_its_players_suggests(db_session: Session) -> None:
    """Summing the simulations rather than the means is the whole reason to run
    one: correlated teammates make a squad's good and bad weeks arrive together,
    so its spread is wider than an independence assumption would give."""
    projections, event = _projections(db_session)
    samples = simulate_event(projections, event, simulations=4000, seed=9)

    club = projections[0].team_id
    starters = [p.player_id for p in projections if p.team_id == club][:11]
    outcome = summarize_squad(samples, starters, captain_id=starters[0], captain_name="C")

    independent = float(np.sqrt(sum(np.var(samples[pid]) for pid in starters)))
    assert outcome.stdev > independent
    assert outcome.percentiles[5] < outcome.median < outcome.percentiles[95]
    assert outcome.captain_name == "C"


def test_captaincy_doubles_a_players_contribution(db_session: Session) -> None:
    projections, event = _projections(db_session)
    samples = simulate_event(projections, event, simulations=1000, seed=2)
    starters = [p.player_id for p in projections[:11]]

    plain = summarize_squad(samples, starters)
    captained = summarize_squad(samples, starters, captain_id=starters[0])

    assert captained.mean == pytest.approx(
        plain.mean + float(np.mean(samples[starters[0]])), rel=1e-6
    )


def test_a_summary_reports_a_floor_below_its_ceiling(db_session: Session) -> None:
    projections, event = _projections(db_session)
    samples = simulate_event(projections, event, simulations=2000, seed=4)

    best = projections[0]
    outcome = summarize_player(best.player_id, best.web_name, samples[best.player_id])

    assert outcome.floor <= outcome.median <= outcome.ceiling
    assert 0.0 <= outcome.haul_probability <= 1.0
    assert 0.0 <= outcome.blank_probability <= 1.0


def test_summarizing_nothing_is_an_error(db_session: Session) -> None:
    with pytest.raises(ValueError):
        summarize_squad({}, [])
