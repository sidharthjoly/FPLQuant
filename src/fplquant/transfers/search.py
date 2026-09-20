"""Transfers as a search from the position you are actually in.

A chess engine does not ask "which single move gains the most material this
move". It searches ahead from the current position, reports the best line, and
tells you what the alternatives are worth. `transfers/planner.py` asks the
first question: one gameweek deep, from the squad you own, "is this swap worth
-4 right now". That is a one-ply search with a myopic evaluation, and it has
the failure a one-ply search always has — it cannot see that holding is a move.
A banked free transfer is worth exactly what it buys you *later*, and later is
not in its model.

`optimizer/multiperiod.py` already searches the horizon properly, with
transfers, banking, hits, captaincy and chips as decision variables. This
module points it at the question a manager actually asks each week — *what do
I do now* — and asks it for more than one answer:

- the **hold line**, which buys nothing this week. Not a fallback: it is the
  baseline every other line is scored against, so "worth it" becomes a
  difference between two evaluations rather than an assertion.
- the **best line**, and then as many distinct alternatives as the time budget
  allows, each one required to drop at least one player the line above it
  bought, so they are real alternatives rather than the same move with a spare
  part bolted on.

Every line carries its whole continuation, because the justification for this
week's move usually lives in a later gameweek, and a recommendation you cannot
interrogate is not much better than a hunch.

The two engines do disagree, which is why this exists. Checked on three real
squads on 2026-09-20: one-ply wanted Saka out where the horizon wanted Barnes
out; on another it took a -8 across three moves where the horizon took -4 on
two entirely different ones.
"""

import logging
import time
from dataclasses import dataclass, replace

from fplquant.optimizer.multiperiod import (
    FREE_HIT,
    WILDCARD,
    HorizonCandidate,
    MultiPeriodPlan,
    plan_horizon,
)
from fplquant.optimizer.types import InfeasibleSquadError, PlayerCandidate, SquadConstraints

logger = logging.getLogger(__name__)

# How many lines to offer, best first, when nothing else caps it.
DEFAULT_LINES = 3
# Wall-clock ceiling for the whole search. Each line is a fresh integer
# program over the full horizon, so an unbounded search is minutes of solving
# on an endpoint that should answer in seconds. The best line and the hold
# line are always attempted; alternatives stop when this runs out and the
# result says how many were reached, so a short answer cannot be mistaken for
# "there was nothing else worth doing".
DEFAULT_SEARCH_SECONDS = 25.0


@dataclass(frozen=True)
class MoveLine:
    """One playable line from the current position, with its continuation.

    `objective` is the discounted quantity the solver maximizes and is what
    the lines are ordered by; `gain_vs_hold` is a difference of it, and is the
    engine's evaluation of the move.

    `horizon_points` is the undiscounted total over the same gameweeks, and it
    can disagree with the ordering — a real line from a real squad ranked
    first on +0.10 of objective while totalling 232.7 undiscounted against the
    hold line's 233.2. That is not a bug in either number. Later gameweeks are
    discounted because only this week's move is executed and the rest is
    re-solved before it arrives, so a line that brings points forward is
    genuinely preferable even when the raw five-week total is a touch lower.
    Show `gain_vs_hold` as the verdict and `horizon_points` as context, not
    the other way around.
    """

    transfers_in: list[PlayerCandidate]
    transfers_out: list[PlayerCandidate]
    hit_cost: int
    horizon_points: float
    objective: float
    gain_vs_hold: float  # against the hold line, in the units the ranking uses
    plan: MultiPeriodPlan

    @property
    def is_hold(self) -> bool:
        return not self.transfers_in

    @property
    def worth_it(self) -> bool:
        """Whether doing this beats banking the transfer, net of any hit."""
        return self.gain_vs_hold > 0


@dataclass(frozen=True)
class TransferSearch:
    """What the search found, and how hard it looked.

    `lines` is ordered best first and always contains the hold line somewhere
    — a manager is entitled to see what doing nothing is worth, and if holding
    *is* the best move it should be at the top rather than absent.
    """

    lines: list[MoveLine]
    hold: MoveLine
    searched: int  # integer programs actually solved
    truncated: bool  # ...and whether the time budget stopped it early

    @property
    def best(self) -> MoveLine:
        return self.lines[0]


def _first_week(plan: MultiPeriodPlan, hold_objective: float | None) -> MoveLine:
    first = plan.gameweeks[0]
    return MoveLine(
        transfers_in=list(first.transfers_in),
        transfers_out=list(first.transfers_out),
        hit_cost=first.hit_cost,
        horizon_points=plan.total_expected_points,
        objective=plan.objective_value,
        gain_vs_hold=(0.0 if hold_objective is None else plan.objective_value - hold_objective),
        plan=plan,
    )


def search_transfer_lines(
    candidates: list[HorizonCandidate],
    events: list[int],
    budget: int,
    current_squad_ids: set[int],
    free_transfers: int = 1,
    constraints: SquadConstraints | None = None,
    decay: float = 0.9,
    chips: frozenset[str] = frozenset(),
    lines: int = DEFAULT_LINES,
    solver_time_limit: int = 60,
    search_seconds: float = DEFAULT_SEARCH_SECONDS,
) -> TransferSearch:
    """Best moves from this squad, ordered, each scored against holding.

    Solves the horizon once for the hold line, once for the best line, then
    once more per alternative, each excluding the signings of every line found
    before it.
    """
    started = time.monotonic()

    def solve(**kwargs: object) -> MultiPeriodPlan:
        return plan_horizon(
            candidates,
            events,
            budget=budget,
            current_squad_ids=current_squad_ids,
            free_transfers=free_transfers,
            constraints=constraints,
            decay=decay,
            chips=chips,
            solver_time_limit=solver_time_limit,
            **kwargs,  # type: ignore[arg-type]
        )

    # The baseline first: every other line's gain is a difference against it,
    # so there is no ordering to do until it exists.
    hold = replace(_first_week(solve(max_first_transfers=0), None), gain_vs_hold=0.0)

    found: list[MoveLine] = []
    excluded: list[frozenset[int]] = []
    truncated = False
    solved = 1

    # The unconstrained best. It may itself be "hold", and that is a real
    # answer — but it leaves nothing to exclude, so the alternatives below
    # have to be asked for with a move forced instead.
    best = _first_week(solve(), hold.objective)
    solved += 1
    if best.transfers_in:
        found.append(best)
        excluded.append(frozenset(player.player_id for player in best.transfers_in))

    while len(found) < lines:
        if time.monotonic() - started > search_seconds:
            truncated = True
            break
        try:
            # A move is forced from here on. Asked without it, the solver
            # replies "hold" to every question after the first, and the
            # second-best move — the thing being asked for — never appears.
            plan = solve(min_first_transfers=1, exclude_first_buys=tuple(excluded))
        except InfeasibleSquadError:
            # Every distinct line has been cut away: there are no further
            # alternatives, which is an answer rather than a failure.
            break
        solved += 1
        line = _first_week(plan, hold.objective)
        bought = frozenset(player.player_id for player in line.transfers_in)
        if not bought:
            break  # cutting on an empty set would forbid nothing
        found.append(line)
        excluded.append(bought)

    ordered = sorted([*found, hold], key=lambda line: -line.objective)
    logger.debug(
        "Transfer search: %d line(s) from %d solves in %.1fs",
        len(ordered),
        solved,
        time.monotonic() - started,
    )
    return TransferSearch(lines=ordered, hold=hold, searched=solved, truncated=truncated)


# Chips whose benefit is a *permanent* change to the squad rather than one
# week's scoring. Their total gain over a fixed horizon declines mechanically
# with the week they are played in — a wildcard in the first week of the
# window improves every week in it, one in the last improves one — so the
# ranking by total is an artifact of where the window ends, not a judgement
# about fixtures. Measured on two real squads over eight gameweeks: +24.28,
# +19.42, +12.27, +6.72, +3.58, +2.76, +1.01, +0.60, perfectly monotone.
PERSISTENT_CHIPS = frozenset({WILDCARD})
# How many gameweeks of payoff to compare on, so that every candidate week is
# judged over the same amount of football.
DEFAULT_PAYOFF_WEEKS = 3


@dataclass(frozen=True)
class ChipWeek:
    """What one chip is worth in one gameweek, against not playing it at all.

    `event` is None for the baseline row — the line where the chip stays in
    your pocket. It is ranked alongside the others rather than left implicit,
    because "none of these weeks is worth it" is the most common honest answer
    to a five-week question about a season-long chip.

    Two measures, and for a wildcard they say different things.
    `gain_vs_holding_the_chip` is the whole-horizon difference and is the one
    biased by where the window ends. `window_gain` is the same comparison over
    a fixed number of gameweeks starting at the play week, so every candidate
    is judged over the same amount of football — it is None only when the
    window would run past the end of the horizon, which is exactly when there
    is not enough projection left to judge that week at all.
    """

    chip: str
    event: int | None
    gain_vs_holding_the_chip: float
    window_gain: float | None
    horizon_points: float
    objective: float
    plan: MultiPeriodPlan

    @property
    def is_baseline(self) -> bool:
        return self.event is None


@dataclass(frozen=True)
class ChipSearch:
    """Every gameweek in the horizon, ranked by what the chip is worth there.

    `at_horizon_edge` is the warning that matters. If the best week is the
    last one the search can see, the real answer is probably outside the
    window — a wildcard's value usually lives in a fixture swing further out
    than five gameweeks — and the honest response is to extend the horizon
    rather than to play the chip on Saturday. The planner already reasons this
    way about the free hit, which it refuses to place in the final week
    because the cost of reverting falls outside the model.
    """

    chip: str
    weeks: list[ChipWeek]
    baseline: ChipWeek
    searched: int
    truncated: bool
    payoff_weeks: int

    @property
    def best(self) -> ChipWeek:
        return self.weeks[0]

    @property
    def comparable(self) -> list[ChipWeek]:
        """Weeks judged over the same amount of football, best first.

        Shorter than `weeks`: a week too close to the end of the horizon has
        no full payoff window and is dropped rather than being compared on
        less evidence than its rivals.
        """
        scored = [week for week in self.weeks if week.window_gain is not None]
        return sorted(scored, key=lambda week: -(week.window_gain or 0.0))

    @property
    def horizon_biased(self) -> bool:
        """Whether ranking these by total gain would mislead. See
        `PERSISTENT_CHIPS`."""
        return self.chip in PERSISTENT_CHIPS

    @property
    def at_horizon_edge(self) -> bool:
        return (
            not self.best.is_baseline
            and not self.truncated
            and self.best.event == max(w.event for w in self.weeks if w.event is not None)
        )

    @property
    def worth_playing(self) -> bool:
        return not self.best.is_baseline


def search_chip_week(
    candidates: list[HorizonCandidate],
    events: list[int],
    budget: int,
    current_squad_ids: set[int],
    chip: str,
    free_transfers: int = 1,
    constraints: SquadConstraints | None = None,
    decay: float = 0.9,
    solver_time_limit: int = 60,
    search_seconds: float = DEFAULT_SEARCH_SECONDS,
    payoff_weeks: int = DEFAULT_PAYOFF_WEEKS,
) -> ChipSearch:
    """Which gameweek to play `chip` in, and what each week is worth.

    Solves the horizon once with the chip unavailable — the baseline — then
    once per gameweek with it pinned there. Weeks are evaluated in order, so a
    search that runs out of time returns a contiguous prefix rather than an
    arbitrary handful, and says it was truncated.

    This answers "which of these gameweeks", not "which gameweek of the
    season". Both are useful; only one of them is being asked here.

    For a wildcard, read `comparable` rather than the raw ranking: a permanent
    upgrade is worth more the earlier it lands inside a fixed window, whatever
    the fixtures do, so the totals decline monotonically and say nothing.
    """
    started = time.monotonic()

    def solve(**kwargs: object) -> MultiPeriodPlan:
        return plan_horizon(
            candidates,
            events,
            budget=budget,
            current_squad_ids=current_squad_ids,
            free_transfers=free_transfers,
            constraints=constraints,
            decay=decay,
            solver_time_limit=solver_time_limit,
            **kwargs,  # type: ignore[arg-type]
        )

    without = solve(chips=frozenset())
    baseline = ChipWeek(
        chip=chip,
        event=None,
        gain_vs_holding_the_chip=0.0,
        window_gain=0.0,
        horizon_points=without.total_expected_points,
        objective=without.objective_value,
        plan=without,
    )

    weeks: list[ChipWeek] = [baseline]
    solved = 1
    truncated = False
    for event in events:
        if event == events[-1] and chip == FREE_HIT:
            # The planner refuses to place a free hit in the final week on
            # purpose: the squad reverts the week after, and that week is
            # outside the model, so the chip would look free. Skipping is not
            # the same answer as "worth nothing", and must not be reported as
            # though it were.
            continue
        if time.monotonic() - started > search_seconds:
            truncated = True
            break
        plan = solve(chips=frozenset({chip}), force_chip_events={chip: event})
        solved += 1
        weeks.append(
            ChipWeek(
                chip=chip,
                event=event,
                gain_vs_holding_the_chip=plan.objective_value - without.objective_value,
                window_gain=_window_gain(plan, without, event, payoff_weeks),
                horizon_points=plan.total_expected_points,
                objective=plan.objective_value,
                plan=plan,
            )
        )

    weeks.sort(key=lambda week: -week.objective)
    logger.debug(
        "Chip search for %s: %d week(s) from %d solves in %.1fs",
        chip,
        len(weeks),
        solved,
        time.monotonic() - started,
    )
    return ChipSearch(
        chip=chip,
        weeks=weeks,
        baseline=baseline,
        searched=solved,
        truncated=truncated,
        payoff_weeks=payoff_weeks,
    )


def _window_gain(
    played: MultiPeriodPlan, baseline: MultiPeriodPlan, event: int, payoff_weeks: int
) -> float | None:
    """Expected points gained over the `payoff_weeks` starting at `event`.

    The same window on both plans, so each candidate week is judged over the
    same amount of football. None when the window runs past the end of the
    horizon: there is not enough projection left to judge that week, and
    scoring it on a shorter window is how a late chip comes out looking cheap.
    """
    weeks = [week.event for week in played.gameweeks]
    if event not in weeks:
        return None
    start = weeks.index(event)
    if start + payoff_weeks > len(weeks):
        return None
    window = slice(start, start + payoff_weeks)
    return sum(week.expected_points for week in played.gameweeks[window]) - sum(
        week.expected_points for week in baseline.gameweeks[window]
    )
