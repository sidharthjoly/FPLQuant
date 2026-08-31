"""Reading a duration or a date out of press wording."""

import datetime as dt

import pytest

from fplquant.news.extract import (
    NONE,
    OUT_FOR_SEASON,
    RETURN_DATE,
    RETURNING,
    RULED_OUT,
    extract_signal,
)

TODAY = dt.date(2026, 8, 31)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Saka will be out for six weeks with a hamstring injury", dt.date(2026, 10, 12)),
        ("Palmer sidelined for up to three months after surgery", dt.date(2026, 11, 29)),
        ("A six-week absence for the midfielder", dt.date(2026, 10, 12)),
        ("Three months on the sidelines for the striker", dt.date(2026, 11, 29)),
        ("Isak expected back on 14 September, says Howe", dt.date(2026, 9, 14)),
        ("Salah returns from injury on September 20", dt.date(2026, 9, 20)),
    ],
)
def test_a_duration_or_a_date_becomes_a_return_date(text: str, expected: dt.date) -> None:
    signal = extract_signal(text, TODAY)
    assert signal.kind == RETURN_DATE
    assert signal.return_date == expected
    assert signal.feeds_the_model


def test_a_range_is_read_at_its_later_end() -> None:
    """Being early about a return is the expensive mistake — it puts a player
    in the squad who is not going to play."""
    signal = extract_signal("Rodri faces two to three weeks out", TODAY)
    assert signal.return_date == TODAY + dt.timedelta(weeks=3)


def test_wording_with_no_time_in_it_never_reaches_the_model() -> None:
    """These are real signals worth storing and showing. None of them contains
    a date, and inventing a recovery curve from one would be exactly the
    improvisation this layer exists to avoid."""
    for text, kind in [
        ("Haaland ruled out for the rest of the season", OUT_FOR_SEASON),
        ("Odegaard is back in training after a shoulder injury", RETURNING),
        ("Guardiola will miss the game against Arsenal", RULED_OUT),
    ]:
        signal = extract_signal(text, TODAY)
        assert signal.kind == kind
        assert not signal.feeds_the_model


def test_football_writing_that_is_not_about_fitness_says_nothing() -> None:
    for text in [
        "Saliba has signed a three-year contract extension",
        "City complete £51m move for striker",
        "The transfers to watch before the window closes",
    ]:
        assert extract_signal(text, TODAY).kind == NONE


def test_an_implausibly_long_absence_is_not_believed() -> None:
    """Past about ten months the sentence is almost always describing a
    contract or a manager's tenure rather than an injury."""
    assert extract_signal("out for twelve months", TODAY).kind == NONE


def test_a_return_date_already_in_the_past_is_ignored() -> None:
    """Feeds carry stories for days and an archive item can resurface. A date
    behind us is not a forecast."""
    assert extract_signal("back on 1 August after injury", TODAY).kind != RETURN_DATE


def test_the_sentence_behind_a_signal_is_kept_for_the_audit_trail() -> None:
    signal = extract_signal(
        "Arsenal won at Anfield. Saka is out for six weeks. The manager spoke after.", TODAY
    )
    assert signal.evidence == "Saka is out for six weeks"
