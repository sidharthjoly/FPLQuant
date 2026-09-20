"""Searching for moves from a real squad position, not just scoring one swap."""

import pytest

from fplquant.optimizer.multiperiod import HorizonCandidate, plan_horizon
from fplquant.optimizer.types import (
    DEFENDER,
    FORWARD,
    GOALKEEPER,
    MIDFIELDER,
    PlayerCandidate,
    SquadConstraints,
)
from fplquant.transfers.search import search_chip_week, search_transfer_lines

EVENTS = [1, 2]
# 2 GKP / 5 DEF / 5 MID / 3 FWD owned, plus replacements to choose between.
_SQUAD_SHAPE = [GOALKEEPER] * 2 + [DEFENDER] * 5 + [MIDFIELDER] * 5 + [FORWARD] * 3


def _candidate(player_id: int, position: int, points: float, cost: int = 40) -> HorizonCandidate:
    return HorizonCandidate(
        candidate=PlayerCandidate(
            player_id=player_id,
            web_name=f"P{player_id}",
            team_id=player_id % 8,
            team_short_name="ARS",
            element_type=position,
            now_cost=cost,
            predicted_points=points,
        ),
        points_by_event=dict.fromkeys(EVENTS, points),
    )


def _pool() -> tuple[list[HorizonCandidate], set[int]]:
    """Fifteen owned players on 2 points, plus better replacements per position."""
    pool = [_candidate(i, position, 2.0) for i, position in enumerate(_SQUAD_SHAPE)]
    owned = {c.player_id for c in pool}
    # Upgrades, each strictly better than the last, all affordable.
    for offset, (position, points) in enumerate(
        [(MIDFIELDER, 9.0), (MIDFIELDER, 8.0), (MIDFIELDER, 7.0), (FORWARD, 6.0)]
    ):
        pool.append(_candidate(100 + offset, position, points))
    return pool, owned


def _search(**kwargs: object):
    pool, owned = _pool()
    budget = sum(c.candidate.now_cost for c in pool if c.player_id in owned) + 100
    return search_transfer_lines(
        pool,
        EVENTS,
        budget=budget,
        current_squad_ids=owned,
        constraints=SquadConstraints(budget=budget, max_per_club=15),
        **kwargs,  # type: ignore[arg-type]
    )


def test_holding_is_a_line_and_is_always_offered() -> None:
    """A one-ply solver cannot value banking a transfer, because the value is
    entirely in what it buys next week. Holding has to be on the list for the
    recommendation to be a comparison rather than an assertion."""
    result = _search(lines=2)

    assert any(line.is_hold for line in result.lines)
    assert result.hold.gain_vs_hold == 0.0
    assert result.hold.hit_cost == 0
    assert not result.hold.transfers_in


def test_lines_come_back_best_first() -> None:
    result = _search(lines=3)

    objectives = [line.objective for line in result.lines]
    assert objectives == sorted(objectives, reverse=True)


def test_an_upgrade_worth_making_beats_holding() -> None:
    result = _search(lines=2)

    assert not result.best.is_hold
    assert result.best.gain_vs_hold > 0
    assert result.best.worth_it


def test_each_alternative_drops_a_signing_the_line_above_it_made() -> None:
    """Otherwise the "alternatives" are the same move with a spare part added,
    which is not another option — it is the same one described twice."""
    result = _search(lines=3)

    moves = [
        frozenset(player.player_id for player in line.transfers_in)
        for line in result.lines
        if not line.is_hold
    ]
    assert len(moves) >= 2
    for earlier_index, earlier in enumerate(moves):
        for later in moves[earlier_index + 1 :]:
            assert not earlier <= later


def test_alternatives_are_still_offered_when_holding_wins() -> None:
    """A manager asking "what are my options" is owed options even when the
    answer is that none of them beats doing nothing. Without forcing a move,
    the solver answers "hold" to every question after the first."""
    pool, owned = _pool()
    # Strip the upgrades: nothing available is better than what is owned.
    pool = [c for c in pool if c.player_id in owned]
    budget = sum(c.candidate.now_cost for c in pool)

    result = search_transfer_lines(
        pool,
        EVENTS,
        budget=budget,
        current_squad_ids=owned,
        constraints=SquadConstraints(budget=budget, max_per_club=15),
        lines=2,
    )

    assert result.best.is_hold
    assert result.lines[0].is_hold
    # ...and anything else it found is correctly scored as worse than holding.
    assert all(line.gain_vs_hold <= 0 for line in result.lines if not line.is_hold)


def test_the_search_reports_how_hard_it_looked() -> None:
    """A truncated search must not read as "there were only two options"."""
    result = _search(lines=3)

    assert result.searched >= 2
    assert result.truncated is False


def test_a_search_budget_of_zero_still_returns_the_best_line_and_the_hold() -> None:
    result = _search(lines=5, search_seconds=0.0)

    assert result.truncated is True
    assert result.best is result.lines[0]
    assert any(line.is_hold for line in result.lines)


def _chip_search(chip: str, **kwargs: object):
    pool, owned = _pool()
    budget = sum(c.candidate.now_cost for c in pool if c.player_id in owned) + 100
    return search_chip_week(
        pool,
        [1, 2, 3, 4],
        budget=budget,
        current_squad_ids=owned,
        chip=chip,
        constraints=SquadConstraints(budget=budget, max_per_club=15),
        **kwargs,  # type: ignore[arg-type]
    )


def test_not_playing_the_chip_is_one_of_the_options() -> None:
    """For a chip held all season, "none of these weeks" is the most common
    honest answer to a question about five of them. It has to be on the list
    rather than implied by its absence."""
    result = _chip_search("bench_boost", payoff_weeks=2)

    assert any(week.is_baseline for week in result.weeks)
    assert result.baseline.gain_vs_holding_the_chip == 0.0
    assert result.baseline.event is None


def test_every_candidate_week_is_judged_over_the_same_amount_of_football() -> None:
    """A week too near the end of the horizon has less payoff left to measure,
    so comparing it on what remains is how a late chip comes out looking cheap
    for reasons that have nothing to do with football."""
    result = _chip_search("bench_boost", payoff_weeks=3)

    # Events are [1, 2, 3, 4], so only weeks 1 and 2 have three gameweeks after
    # them; 3 and 4 cannot be compared and are left out rather than discounted.
    comparable = [week.event for week in result.comparable if not week.is_baseline]
    assert comparable and set(comparable) <= {1, 2}
    assert all(week.window_gain is not None for week in result.comparable)


def test_a_permanent_chip_is_flagged_because_its_total_always_favours_now() -> None:
    """A wildcard improves every week left in the window, so the whole-horizon
    total falls with the week it is played in whatever the fixtures do.
    Ranking on it would always answer "now", which is not an answer."""
    assert _chip_search("wildcard", payoff_weeks=2).horizon_biased
    assert not _chip_search("bench_boost", payoff_weeks=2).horizon_biased


def test_the_free_hit_is_never_offered_in_the_last_week() -> None:
    """Its cost falls the week after, which is outside the model, so the
    planner bans it there. A skipped week is not the same as a worthless one
    and must not be reported as though it were."""
    result = _chip_search("free_hit", payoff_weeks=1)

    assert 4 not in [week.event for week in result.weeks]
    assert not result.truncated


def test_forcing_a_chip_it_was_not_given_is_refused() -> None:
    pool, owned = _pool()
    budget = sum(c.candidate.now_cost for c in pool if c.player_id in owned)
    with pytest.raises(ValueError, match="not in `chips`"):
        plan_horizon(
            pool,
            [1, 2],
            budget=budget,
            current_squad_ids=owned,
            constraints=SquadConstraints(budget=budget, max_per_club=15),
            force_chip_events={"wildcard": 1},
        )


def test_forcing_a_chip_outside_the_horizon_is_refused() -> None:
    pool, owned = _pool()
    budget = sum(c.candidate.now_cost for c in pool if c.player_id in owned)
    with pytest.raises(ValueError, match="outside the horizon"):
        plan_horizon(
            pool,
            [1, 2],
            budget=budget,
            current_squad_ids=owned,
            constraints=SquadConstraints(budget=budget, max_per_club=15),
            chips=frozenset({"wildcard"}),
            force_chip_events={"wildcard": 9},
        )


def test_a_forced_chip_is_actually_played_in_that_week() -> None:
    pool, owned = _pool()
    budget = sum(c.candidate.now_cost for c in pool if c.player_id in owned) + 100
    plan = plan_horizon(
        pool,
        [1, 2, 3],
        budget=budget,
        current_squad_ids=owned,
        constraints=SquadConstraints(budget=budget, max_per_club=15),
        chips=frozenset({"bench_boost"}),
        force_chip_events={"bench_boost": 2},
    )

    played = {gameweek.event: gameweek.chip for gameweek in plan.gameweeks}
    assert played[2] == "bench_boost"
    assert played[1] is None and played[3] is None
