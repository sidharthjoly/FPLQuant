"""How likely a player is to be fit and eligible, gameweek by gameweek.

Everywhere else in this codebase availability is a single number. It is applied
to the next fixture, which is right, and then reused unchanged for every other
fixture in a five-week projection, which is not: `project_horizon` computes
usage once and hands the same `p_start` to a match in November as to one on
Saturday. A player ruled out until the 10th is therefore ruled out until
February, and a player carrying a knock this week is still carrying it a month
from now. Both are wrong in ways that change which squad the optimizer picks
and, more sharply, whether the transfer planner tells you to sell somebody.

This module supplies the missing dimension, under two rules it holds to
absolutely.

**It cannot contradict FPL about the next round.** The first event in the
horizon comes back as exactly `form.fixtures.chance_of_playing`, taken verbatim
rather than recomputed. FPL's percentage *is* the press-conference summary, so
re-reading the same text and applying a second discount would charge the same
evidence twice — the mistake `lineup.starts` and `form.fixtures` both warn
about at length. The layer earns its keep only on events 2..N, where the
percentage has nothing to say by construction.

**It can only ever give availability back.** Every projection is floored at the
published number, so this can restore a suspended player once his ban expires
but can never newly rule anyone out. That asymmetry is deliberate: availability
is a hard gate, and a spurious zero silently deletes a fit player from the
squad, where a spurious one merely makes the model slightly optimistic about
somebody FPL has already flagged.

What is left, then, is time — and the three ways it runs differently:

- A **suspension** ends on a date that is *known*. Once it expires the player
  is fully fit, so this steps cleanly to 1.0 with no hedging.
- An **injury with a stated return date** ends on a date that is a club's
  estimate, and estimates slip late far more often than early. So the stated
  date is read as optimistic and the recovery ramps in over a window that
  widens the further out the forecast is.
- A **doubt** — a knock, with a percentage and no date — is a statement about
  one match. It resolves; the model just doesn't know which way. Availability
  recovers toward the ceiling below over a window set by how bad the doubt is.

Everything else keeps the published number at every event: an injury with no
return date, and any news the parser could not read.
"""

import datetime as dt
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from fplquant.models.orm import Fixture, Player
from fplquant.news.items import NewsCategory, NewsSource, PlayerNews
from fplquant.news.sources import default_sources
from fplquant.schedule import get_upcoming_fixtures_by_team_event

# The most availability any *projected* recovery is allowed to reach. A fully
# fit player with no news gets 1.0; somebody the model is only guessing is fit
# again does not, because the guess can be wrong in a way that plain fitness
# cannot: return dates slip, knocks recur, and setbacks are commonest in the
# players already carrying something. It is also self-limiting in practice —
# by the time the gameweek arrives FPL has published a real status and this
# projection has been replaced by fact, so the discount only ever applies to
# rounds that haven't happened yet.
MAX_PROJECTED_AVAILABILITY = 0.9

# How long, in days, a stated return date takes to become a full recovery, and
# how much that stretches with the forecast horizon. A club saying "back in a
# week" is a much firmer statement than one saying "back in three months", and
# the second deserves a far wider band. At 10 + 0.25 x lead, a return five days
# out resolves over about eleven days, and one three months out over a month.
RETURN_SLIP_BASE_DAYS = 10.0
RETURN_SLIP_LEAD_FRACTION = 0.25

# How much wider the slippage window is around a return date that came from a
# press report rather than from FPL. A journalist relaying "six weeks" is a real
# estimate and a second-hand one, so it is believed more slowly — the ramp is
# the same shape, stretched.
REPORTED_RETURN_SLIP_MULTIPLIER = 1.5

# Days for a doubt to clear completely if FPL rated the player's chance of
# playing at zero. Scaled by the published percentage, so the familiar 75% is
# resolved in a fortnight, 50% in a month, and 25% in six weeks — which is
# about how those three grades actually behave.
DOUBT_RECOVERY_DAYS = 56.0

# Availability at which a player counts as "back" for display purposes.
RETURN_EVENT_THRESHOLD = 0.5


@dataclass(frozen=True)
class PlayerAvailability:
    """One player's news and what it implies for each event in the horizon."""

    player_id: int
    news: PlayerNews
    by_event: dict[int, float]
    # The first event the player is more likely than not to be available for,
    # or None if they are available already or not back inside the horizon.
    return_event: int | None

    @property
    def is_time_varying(self) -> bool:
        """Whether this player's availability actually moves over the horizon.

        The honest headline for a UI: true for the handful of players the news
        says something datable about, false for everyone else — including
        players who are definitely out but whose news carries no return date.
        """
        values = set(self.by_event.values())
        return len(values) > 1


def _event_reference_dates(session: Session, events: list[int]) -> dict[int, dt.date]:
    """The earliest kickoff in each event, as a stand-in for clubs that blank.

    A club with no fixture in a round still needs *some* availability, because
    `engine.usage` normalises attacking shares across a whole squad whether or
    not that squad plays. Using the round's own earliest kickoff keeps them on
    the same clock as everybody else instead of freezing them at today.
    """
    rows = (
        session.query(Fixture.event, Fixture.kickoff_time)
        .filter(
            Fixture.event.in_(events),
            Fixture.kickoff_time.isnot(None),
            Fixture.finished.is_(False),
        )
        .all()
    )
    reference: dict[int, dt.date] = {}
    for event, kickoff in rows:
        if kickoff is None:
            continue
        day = kickoff.date()
        if event not in reference or day < reference[event]:
            reference[event] = day
    return reference


def _kickoff_dates_by_team(
    session: Session, events: list[int], reference: dict[int, dt.date], as_of: dt.date
) -> dict[int, dict[int, dt.date]]:
    """team_id -> event -> the day that club's round is played on.

    Per club, not per league, because a gameweek is spread over three or four
    days and the whole signal lives on that boundary: a ban expiring on the
    19th clears a club playing Sunday the 20th and not one playing Friday the
    18th, and both of those are the same gameweek.

    A double gameweek is represented by its *earlier* kickoff, which understates
    availability for the second match of a return week. That is the direction to
    be wrong in, and the alternative — availability per fixture rather than per
    event — is not expressible while `engine.usage` produces one profile per
    player per event.
    """
    fixtures_by_team_event = get_upcoming_fixtures_by_team_event(session)
    dates: dict[int, dict[int, dt.date]] = {}
    for team_id, by_event in fixtures_by_team_event.items():
        per_event: dict[int, dt.date] = {}
        for event in events:
            kickoffs = [f.kickoff_time.date() for f in by_event.get(event, []) if f.kickoff_time]
            if kickoffs:
                per_event[event] = min(kickoffs)
            else:
                per_event[event] = reference.get(event, as_of)
        dates[team_id] = per_event
    return dates


def projected_availability(
    news: PlayerNews, kickoff: dt.date, as_of: dt.date, anchored_on: dt.date
) -> float:
    """Availability for a match played on `kickoff`, given news read on `as_of`.

    `anchored_on` is the day of the match FPL's published percentage actually
    describes — this player's club's next one. Recovery from a doubt accrues
    from *there*, not from today, because the percentage is already a statement
    about the player's condition on that day; measuring from today would credit
    a recovery that the published number has priced in.

    Floored at the published next-round number in every branch, so the layer can
    restore availability over time but never remove more of it — see the module
    docstring for why that asymmetry is the safe one.
    """
    anchor = news.next_round_availability

    if news.category is NewsCategory.DEPARTED:
        # Out of the league. No date brings them back and none is ever stated.
        return 0.0

    if news.category is NewsCategory.SUSPENDED:
        if news.return_date is None:
            return anchor
        # A served ban leaves a fit player, and the expiry is a fact rather than
        # a forecast — so this steps rather than ramps, and steps to full
        # availability rather than to the projected ceiling.
        return 1.0 if kickoff >= news.return_date else anchor

    if news.category is NewsCategory.INJURED:
        if news.return_date is None:
            return anchor
        elapsed = (kickoff - news.return_date).days
        if elapsed <= 0:
            return anchor
        lead = max(0, (news.return_date - as_of).days)
        window = RETURN_SLIP_BASE_DAYS + RETURN_SLIP_LEAD_FRACTION * lead
        if news.return_date_is_reported:
            window *= REPORTED_RETURN_SLIP_MULTIPLIER
        ramp = MAX_PROJECTED_AVAILABILITY * min(1.0, elapsed / window)
        return max(anchor, ramp)

    if news.category is NewsCategory.DOUBT:
        if anchor >= MAX_PROJECTED_AVAILABILITY:
            return anchor
        window = DOUBT_RECOVERY_DAYS * (1.0 - anchor)
        if window <= 0:
            return anchor
        elapsed = max(0, (kickoff - anchored_on).days)
        recovered = anchor + (MAX_PROJECTED_AVAILABILITY - anchor) * min(1.0, elapsed / window)
        return max(anchor, recovered)

    # AVAILABLE, and UNKNOWN — news we could not read says nothing, which is
    # the branch that keeps a change to FPL's wording from inventing dates.
    return anchor


def merge_reported_return(official: PlayerNews, reported: PlayerNews) -> PlayerNews:
    """Fold a feed-derived item into FPL's official one.

    FPL wins on everything except the single field it leaves blank. A
    supplementary source may supply a return date, and only when *all* of these
    hold:

    - FPL has not given one. An official return date is the club's own line and
      is never overridden by a report of it.
    - The absence is an injury. A ban's length is a matter of record and FPL
      states it; press speculation about one is not an improvement.
    - The report actually carries a date. Everything else it might say —
      "back in training", "ruled out" — has no date to build a recovery from.

    Category, condition and next-round availability always come from FPL. This
    is what makes it impossible for a misresolved article to rule anybody out:
    the only field it can touch is one that currently reads "unknown", and the
    only direction the resulting projection can move is up.
    """
    if official.return_date is not None:
        return official
    if official.category is not NewsCategory.INJURED:
        return official
    if reported.return_date is None:
        return official
    return replace(
        official,
        return_date=reported.return_date,
        return_is_certain=False,
        return_date_is_reported=True,
        source=f"{official.source}+{reported.source}",
        headline=f"{official.headline} — {reported.headline}".strip(" —"),
    )


def availability_timeline(
    session: Session,
    events: list[int],
    *,
    as_of: dt.datetime | None = None,
    sources: tuple[NewsSource, ...] | None = None,
) -> dict[int, PlayerAvailability]:
    """Per-player news and its per-event availability, keyed by player id.

    `events` is the horizon as `fplquant.schedule.upcoming_events` returns it.
    FPL's published percentage is taken to describe each player's club's *next
    match* within that horizon, which is usually `events[0]` and is not always:
    a single postponed fixture keeps a round alive for the nineteen clubs not
    playing in it. Every event up to and including that match returns the
    published number exactly, by construction rather than by arithmetic — so
    `events[0]` always does, whichever club it is.
    """
    if not events:
        return {}

    as_of = as_of or dt.datetime.now(dt.UTC)
    today = as_of.date()
    sources = default_sources() if sources is None else sources

    news_by_player: dict[int, PlayerNews] = {}
    for source in sources:
        for item in source.fetch(session, today):
            if source.authoritative:
                news_by_player[item.player_id] = item
                continue
            official = news_by_player.get(item.player_id)
            # A supplementary item about a player no authoritative source has
            # spoken about is dropped rather than promoted. It carries no
            # next-round availability of its own, and inventing one is exactly
            # the thing this layer is not allowed to do.
            if official is not None:
                news_by_player[item.player_id] = merge_reported_return(official, item)
    if not news_by_player:
        return {}

    reference = _event_reference_dates(session, events)
    kickoffs = _kickoff_dates_by_team(session, events, reference, today)
    team_by_player = _team_by_player(session)

    fixture_events = _fixture_events_by_team(session, events)

    timeline: dict[int, PlayerAvailability] = {}
    for player_id, news in news_by_player.items():
        team_id = team_by_player.get(player_id, -1)
        team_kickoffs = kickoffs.get(team_id, {})
        # The round FPL's percentage is actually about: this club's next match,
        # which is not always `events[0]`. A round can be almost entirely blank
        # — a single postponed fixture keeps it in the horizon — and for the
        # nineteen clubs not playing in it, "next round" means the one after.
        # Projecting a recovery across that gap would credit days of healing
        # that the published percentage has already accounted for.
        anchor_event = next((e for e in events if e in fixture_events.get(team_id, ())), events[0])
        anchored_on = team_kickoffs.get(anchor_event, reference.get(anchor_event, today))

        by_event: dict[int, float] = {}
        for event in events:
            if event <= anchor_event:
                # The contract. Not `projected_availability(...)` evaluated at
                # this event's kickoff — the published number itself, so the
                # next round cannot drift from what the rest of the model uses.
                by_event[event] = news.next_round_availability
                continue
            kickoff = team_kickoffs.get(event, reference.get(event, today))
            by_event[event] = projected_availability(news, kickoff, today, anchored_on)

        timeline[player_id] = PlayerAvailability(
            player_id=player_id,
            news=news,
            by_event=by_event,
            return_event=_return_event(by_event, events),
        )
    return timeline


def _fixture_events_by_team(session: Session, events: list[int]) -> dict[int, frozenset[int]]:
    """team_id -> which of `events` that club actually has a fixture in."""
    fixtures_by_team_event = get_upcoming_fixtures_by_team_event(session)
    wanted = set(events)
    return {
        team_id: frozenset(event for event in by_event if event in wanted)
        for team_id, by_event in fixtures_by_team_event.items()
    }


def _return_event(by_event: dict[int, float], events: list[int]) -> int | None:
    """The first round the player is more likely than not to be available for.

    None when they already are, and None when they are not back inside the
    horizon at all — the two cases where naming a round would be misleading
    rather than informative.
    """
    if by_event.get(events[0], 0.0) >= RETURN_EVENT_THRESHOLD:
        return None
    for event in events:
        if by_event[event] >= RETURN_EVENT_THRESHOLD:
            return event
    return None


def _team_by_player(session: Session) -> dict[int, int]:
    return {row[0]: row[1] for row in session.query(Player.id, Player.team_id).all()}


def availability_by_event(
    session: Session,
    events: list[int],
    *,
    as_of: dt.datetime | None = None,
    sources: tuple[NewsSource, ...] | None = None,
) -> dict[int, dict[int, float]]:
    """event -> player_id -> availability. What the engine consumes.

    One vector per event rather than per fixture, since each player is already
    evaluated against their own club's kickoff — so the number of distinct
    vectors is bounded by the horizon, and in practice is far smaller because
    most players are simply available in all of them.
    """
    timeline = availability_timeline(session, events, as_of=as_of, sources=sources)
    return {
        event: {player_id: entry.by_event[event] for player_id, entry in timeline.items()}
        for event in events
    }
