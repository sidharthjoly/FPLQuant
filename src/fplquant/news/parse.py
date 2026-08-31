"""Reading FPL's news strings.

FPL publishes player news as free text, but it is not really free: it is
generated from a small set of templates, and every one of the 118 non-empty
strings in the live database on 2026-08-31 matched one of five.

    Knee injury - Unknown return date
    Ankle injury - Expected back 14 Sep
    Thigh injury - 75% chance of playing
    Suspended until 19 Sep
    Has joined Paris Saint-Germain permanently

Only two of those carry information the rest of the model doesn't already
have — the two with a date. The parser exists to find them without guessing at
anything else, so it is deliberately strict: a string it does not recognise
becomes `NewsCategory.UNKNOWN`, which the availability layer treats as "say
nothing" rather than as an excuse to improvise. FPL changes this text
occasionally and the failure mode has to be a signal that quietly stops firing,
never one that starts inventing return dates.
"""

import datetime as dt
import re
from dataclasses import dataclass

from fplquant.news.items import NewsCategory

# How far into the past a bare "5 Sep" is allowed to resolve before it is read
# as next year's instead. News dates are about the near future, so a date that
# would land months behind us is a year boundary, not a stale item — this is
# what makes "Expected back 5 Jan", published in December, resolve correctly.
_YEAR_ROLLOVER_DAYS = 90

_PERCENT = re.compile(r"^(?P<condition>.+?)\s*-\s*(?P<percent>\d+)%\s*chance of playing$", re.I)
_EXPECTED_BACK = re.compile(r"^(?P<condition>.+?)\s*-\s*Expected back\s+(?P<date>.+?)$", re.I)
_UNKNOWN_RETURN = re.compile(r"^(?P<condition>.+?)\s*-\s*Unknown return date$", re.I)
_SUSPENDED_UNTIL = re.compile(r"^Suspended(?:\s+until)?\s+(?P<date>\d{1,2}\s+\w+)$", re.I)
# "Has joined Juventus on loan...", "Has joined Barcelona permanently",
# "has returned to Getafe CF" — a player who has left the league entirely.
_DEPARTED = re.compile(r"^(?:Has joined|Has returned to|Has left)\b", re.I)

# Words that mark a suspension when they turn up on the left of the dash, e.g.
# a future "Suspended - 0% chance of playing". Kept separate from the
# `Suspended until` pattern because only that one carries an end date.
_SUSPENSION_WORDS = ("suspend", "ban")


@dataclass(frozen=True)
class ParsedNews:
    """Result of reading one news string. See `parse_news`."""

    category: NewsCategory
    condition: str | None = None
    return_date: dt.date | None = None


def resolve_partial_date(text: str, as_of: dt.date) -> dt.date | None:
    """A bare "14 Sep" or "5 September" as an actual date, or None.

    FPL omits the year, which is unambiguous to a reader and not to a parser.
    The year is chosen as the one that puts the date in the near future, which
    is the only kind of date this text ever contains: a return or a ban expiry.
    """
    text = text.strip().rstrip(".")
    parsed = None
    for pattern in ("%d %b %Y", "%d %B %Y"):
        try:
            # Parsed against a leap year so "29 Feb" yields a month and day at
            # all; whether it exists in the *target* year is settled below,
            # where there is somewhere sensible for the failure to go.
            parsed = dt.datetime.strptime(f"{text} 2000", pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        return None

    try:
        candidate = dt.date(as_of.year, parsed.month, parsed.day)
    except ValueError:
        # 29 February in a non-leap year — the only day this can fail on.
        return None
    if (as_of - candidate).days > _YEAR_ROLLOVER_DAYS:
        try:
            candidate = dt.date(as_of.year + 1, parsed.month, parsed.day)
        except ValueError:
            return None
    return candidate


def _category_from_status(status: str) -> NewsCategory:
    """What FPL's one-letter status says on its own, with no text to read.

    The fallback when there is no news string, or when there is one and it
    doesn't parse. "a" is available; "d" is a doubt; "s" suspended; "u"
    unavailable, which in practice means gone from the league. "i" and anything
    else are treated as injured.
    """
    return {
        "a": NewsCategory.AVAILABLE,
        "d": NewsCategory.DOUBT,
        "s": NewsCategory.SUSPENDED,
        "u": NewsCategory.DEPARTED,
    }.get(status, NewsCategory.INJURED)


def parse_news(text: str, *, status: str, as_of: dt.date) -> ParsedNews:
    """Read one published news string.

    `status` is FPL's one-letter code, used both as the fallback when there is
    nothing to read and as the tie-breaker on the shapes that don't say what
    kind of absence they are — "Unknown return date" reads the same for a
    suspension as for a torn hamstring.

    Never raises. Anything unrecognised comes back as `UNKNOWN` with no return
    date, which the availability layer treats as no information at all.
    """
    text = (text or "").strip()
    if not text:
        return ParsedNews(_category_from_status(status))

    if _DEPARTED.match(text):
        return ParsedNews(NewsCategory.DEPARTED, condition=text)

    suspended = _SUSPENDED_UNTIL.match(text)
    if suspended:
        return ParsedNews(
            NewsCategory.SUSPENDED,
            condition="Suspension",
            return_date=resolve_partial_date(suspended.group("date"), as_of),
        )

    percent = _PERCENT.match(text)
    if percent:
        condition = percent.group("condition").strip()
        # The percentage itself is deliberately *not* read back out here: it is
        # already on the player row as `chance_of_playing_next_round`, and that
        # column is what the anchor is taken from. Reading it twice invites the
        # two drifting apart.
        return ParsedNews(_absence_category(condition, status, NewsCategory.DOUBT), condition)

    expected = _EXPECTED_BACK.match(text)
    if expected:
        condition = expected.group("condition").strip()
        return ParsedNews(
            _absence_category(condition, status, NewsCategory.INJURED),
            condition,
            resolve_partial_date(expected.group("date"), as_of),
        )

    unknown_return = _UNKNOWN_RETURN.match(text)
    if unknown_return:
        condition = unknown_return.group("condition").strip()
        return ParsedNews(_absence_category(condition, status, NewsCategory.INJURED), condition)

    return ParsedNews(NewsCategory.UNKNOWN, condition=text)


def _absence_category(condition: str, status: str, default: NewsCategory) -> NewsCategory:
    """Whether an absence is a ban or a fitness problem.

    A suspension is read off either side of the string — the condition text
    where FPL spells it out, otherwise the status code — because the two carry
    completely different return dynamics and confusing them is the one mistake
    in this module with real consequences.
    """
    if any(word in condition.lower() for word in _SUSPENSION_WORDS):
        return NewsCategory.SUSPENDED
    if status == "s":
        return NewsCategory.SUSPENDED
    if status == "u":
        return NewsCategory.DEPARTED
    return default
