"""The shape of one piece of news, and where news comes from."""

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session


class NewsCategory(StrEnum):
    """What a piece of news says about a player's availability.

    The distinction that earns its keep is `SUSPENDED` against `INJURED`: a ban
    ends on a date that is *known*, and a player serving one is fully fit the
    moment it expires. An injury return date is a club's estimate and estimates
    slip. `fplquant.news.availability` treats the two very differently and
    would be wrong to blend them.
    """

    AVAILABLE = "available"  # nothing to report
    DOUBT = "doubt"  # carrying a knock; FPL has published a percentage
    INJURED = "injured"  # ruled out, with or without a stated return date
    SUSPENDED = "suspended"  # serving a ban that expires on a known date
    DEPARTED = "departed"  # transferred or loaned out of the league entirely
    UNKNOWN = "unknown"  # there is news, and we could not read it


@dataclass(frozen=True)
class PlayerNews:
    """One player's current news, parsed.

    `next_round_availability` is the anchor the whole layer is pinned to: it is
    whatever `fplquant.form.fixtures.chance_of_playing` returns, carried along
    so the projection can never contradict FPL about the round FPL was talking
    about. See the package docstring.
    """

    player_id: int
    category: NewsCategory
    headline: str  # the text exactly as published, for display and for audit
    condition: str | None  # "Knee injury", "Knock", "Lack of match fitness"
    return_date: dt.date | None  # stated return, where the news gives one
    return_is_certain: bool  # true only for a served ban, whose end date is fixed
    next_round_availability: float  # 0.0-1.0, and authoritative for the next round
    source: str
    # True when the return date came from a press report rather than from FPL.
    # A journalist's "out for six weeks" is a real estimate and a weaker one
    # than the club's own line, so `availability` widens the slippage window
    # around it instead of treating the two as interchangeable.
    return_date_is_reported: bool = False


class NewsSource(Protocol):
    """Somewhere player news comes from.

    `authoritative` is the field that matters. Exactly one source — FPL's own —
    is entitled to say what a player's availability *is*; everything else is
    supplementary and may only fill in a blank the authoritative source left,
    which today means a return date for a player ruled out without one. The
    consumer (`availability`) enforces that, so a new source is a new file
    rather than a change to the model, and cannot widen its own remit.
    """

    name: str
    authoritative: bool

    def fetch(self, session: Session, as_of: dt.date) -> list[PlayerNews]:
        """Current news for every player this source knows about."""
        ...
