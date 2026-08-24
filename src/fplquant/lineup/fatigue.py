import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.models.orm import Player
from fplquant.schedule import get_next_fixture_by_team

# A normal weekly turnaround. At or above this a player is treated as fully
# rested, so the ordinary one-match-per-week schedule carries no penalty at all.
FRESH_REST_DAYS = 7.0
# A midweek turnaround. At or below this, rest is as short as the fixture list
# ever really makes it, and rotation is at its most likely.
CONGESTED_REST_DAYS = 3.0
# How many recent gameweeks count toward minutes load.
LOAD_WINDOW = 3


@dataclass(frozen=True)
class FatigueScore:
    player_id: int
    web_name: str
    rest_days: float | None  # None when we can't see the last match or the next one
    minutes_load: float  # 0.0-1.0, share of available minutes played recently
    fatigue_index: float  # 0.0 (fresh) - 1.0 (short turnaround after a full workload)


def _congestion(rest_days: float | None) -> float:
    """How short this turnaround is, on a 0.0 (normal week) - 1.0 (midweek) scale."""
    if rest_days is None:
        return 0.0
    if rest_days >= FRESH_REST_DAYS:
        return 0.0
    if rest_days <= CONGESTED_REST_DAYS:
        return 1.0
    return (FRESH_REST_DAYS - rest_days) / (FRESH_REST_DAYS - CONGESTED_REST_DAYS)


def compute_fatigue_scores(
    session: Session,
    load_window: int = LOAD_WINDOW,
    players: list[Player] | None = None,
) -> list[FatigueScore]:
    """How heavily each player has been worked going into their next match.

    Fatigue is the product of two things, and it needs both: a short turnaround
    only matters for someone who actually played, and a heavy workload only
    matters if the next match comes around quickly. A squad player who sat on
    the bench through a midweek game is not tired, and a player who went 90
    minutes ten days ago is rested. So:

        fatigue_index = congestion(rest_days) * minutes_load

    `rest_days` is measured from the player's own last *appearance* (not their
    team's last fixture) to their team's next kickoff, and `minutes_load` is the
    share of available minutes they played over the last `load_window`
    gameweeks. Both are needed for the index to be non-zero.

    This is deliberately only used to adjust how likely a player is to be
    *picked* — see `fplquant.lineup.starts`. We do not model fatigue degrading
    a player's per-90 output: with a handful of gameweeks on record that effect
    cannot be separated from ordinary variance, and a hand-tuned coefficient
    for it would be unfalsifiable.
    """
    next_fixture_by_team = get_next_fixture_by_team(session)
    # `players` is accepted pre-loaded so a caller that already has every
    # player's gameweek history in memory doesn't pay to walk it again.
    if players is None:
        players = session.query(Player).options(selectinload(Player.gameweek_stats)).all()

    scores = []
    for player in players:
        stats = sorted(player.gameweek_stats, key=lambda s: s.round)
        played = [s for s in stats if s.minutes > 0]

        fixture = next_fixture_by_team.get(player.team_id)
        next_kickoff = fixture.kickoff_time if fixture else None
        last_kickoff = played[-1].kickoff_time if played else None

        if next_kickoff is None or last_kickoff is None:
            rest = None
        else:
            # Kickoff times come off the FPL API in UTC; normalize so naive rows
            # written by older ingests can't raise on the subtraction.
            if next_kickoff.tzinfo is None:
                next_kickoff = next_kickoff.replace(tzinfo=dt.UTC)
            if last_kickoff.tzinfo is None:
                last_kickoff = last_kickoff.replace(tzinfo=dt.UTC)
            delta = (next_kickoff - last_kickoff).total_seconds() / 86400
            # A non-positive gap means the calendar and the history disagree —
            # the "next" fixture is one the player has already played, usually
            # because a finished fixture hasn't been marked finished yet. Report
            # that as unknown rather than as a zero-day turnaround, which would
            # otherwise read as maximum fatigue and quietly cut the player's
            # start odds on nothing more than stale data.
            rest = delta if delta > 0 else None

        window = stats[-load_window:]
        minutes_load = sum(s.minutes for s in window) / (90 * len(window)) if window else 0.0
        minutes_load = min(1.0, minutes_load)

        scores.append(
            FatigueScore(
                player_id=player.id,
                web_name=player.web_name,
                rest_days=rest,
                minutes_load=minutes_load,
                fatigue_index=_congestion(rest) * minutes_load,
            )
        )
    return sorted(scores, key=lambda s: s.fatigue_index, reverse=True)
