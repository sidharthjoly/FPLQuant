import datetime as dt

import pytest

from fplquant.news.items import NewsCategory
from fplquant.news.parse import parse_news, resolve_partial_date

TODAY = dt.date(2026, 8, 31)


def parse(text: str, status: str = "i") -> object:
    return parse_news(text, status=status, as_of=TODAY)


@pytest.mark.parametrize(
    ("text", "status", "category", "condition", "return_date"),
    [
        ("Knee injury - Unknown return date", "i", NewsCategory.INJURED, "Knee injury", None),
        (
            "Ankle injury - Expected back 14 Sep",
            "i",
            NewsCategory.INJURED,
            "Ankle injury",
            dt.date(2026, 9, 14),
        ),
        ("Knock - 75% chance of playing", "d", NewsCategory.DOUBT, "Knock", None),
        (
            "Lack of match fitness - 50% chance of playing",
            "d",
            NewsCategory.DOUBT,
            "Lack of match fitness",
            None,
        ),
        ("Suspended until 19 Sep", "s", NewsCategory.SUSPENDED, "Suspension", dt.date(2026, 9, 19)),
        (
            "Has joined Paris Saint-Germain permanently",
            "u",
            NewsCategory.DEPARTED,
            "Has joined Paris Saint-Germain permanently",
            None,
        ),
        (
            "has returned to Getafe CF",
            "u",
            NewsCategory.DEPARTED,
            "has returned to Getafe CF",
            None,
        ),
    ],
)
def test_every_shape_fpl_actually_publishes_is_read(
    text: str,
    status: str,
    category: NewsCategory,
    condition: str,
    return_date: dt.date | None,
) -> None:
    """The five templates, taken from all 118 non-empty news strings in the
    live database on 2026-08-31. Between them they covered every player."""
    parsed = parse_news(text, status=status, as_of=TODAY)
    assert parsed.category is category
    assert parsed.condition == condition
    assert parsed.return_date == return_date


def test_wording_we_have_never_seen_is_not_guessed_at() -> None:
    """The failure mode has to be a signal that stops firing, never one that
    starts inventing return dates — FPL changes this text without notice."""
    parsed = parse("Will undergo a late fitness test ahead of Saturday")
    assert parsed.category is NewsCategory.UNKNOWN
    assert parsed.return_date is None


def test_no_news_falls_back_to_the_status_code() -> None:
    assert parse("", status="a").category is NewsCategory.AVAILABLE
    assert parse("", status="d").category is NewsCategory.DOUBT
    assert parse("", status="i").category is NewsCategory.INJURED
    assert parse("", status="s").category is NewsCategory.SUSPENDED
    assert parse("", status="u").category is NewsCategory.DEPARTED


def test_a_ban_is_never_read_as_an_injury() -> None:
    """The one distinction in this module with real consequences: a ban ends on
    a date that is known, an injury return date is a club's estimate, and
    `availability` treats the two completely differently."""
    assert parse("Suspended - Unknown return date", status="s").category is NewsCategory.SUSPENDED
    # ...read off the text even when the status column disagrees.
    assert parse("Ban - 0% chance of playing", status="d").category is NewsCategory.SUSPENDED
    # ...and off the status when the text is the one that doesn't say so.
    assert parse("Unspecified - Unknown return date", status="s").category is NewsCategory.SUSPENDED
    assert parse("Unspecified - Unknown return date", status="u").category is NewsCategory.DEPARTED


def test_a_bare_day_and_month_resolves_into_the_near_future() -> None:
    assert resolve_partial_date("14 Sep", TODAY) == dt.date(2026, 9, 14)
    assert resolve_partial_date("5 September", TODAY) == dt.date(2026, 9, 5)
    # A date months behind us is next year's, not a stale item — this is what
    # makes "Expected back 5 Jan", published in December, come out right.
    assert resolve_partial_date("5 Jan", dt.date(2026, 12, 20)) == dt.date(2027, 1, 5)
    # ...but a date only just behind us is genuinely just behind us: a return
    # that has slipped past, not one eleven months away.
    assert resolve_partial_date("28 Aug", TODAY) == dt.date(2026, 8, 28)


def test_an_unreadable_date_does_not_take_the_rest_of_the_news_with_it() -> None:
    """A return date we cannot parse should still leave a correctly categorised
    injury, so the player stays ruled out rather than becoming available."""
    parsed = parse("Knee injury - Expected back soon")
    assert parsed.category is NewsCategory.INJURED
    assert parsed.return_date is None
    assert resolve_partial_date("29 Feb", dt.date(2026, 1, 1)) is None
