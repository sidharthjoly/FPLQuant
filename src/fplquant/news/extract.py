"""Reading what an article says about a player's availability.

FPL's own news is templated and can be parsed exactly (`fplquant.news.parse`).
Press writing is not, so this does not try to understand it — it looks for a
small set of phrasings that carry a *duration or a date*, and reports nothing
for everything else. "Nothing" is the overwhelmingly common answer and is the
correct one: most football writing is match reporting, not a fitness bulletin.

Only one of the signals below is ever allowed to move a projection, and it is
worth saying why the others are not:

- `RETURN_DATE` is the one. A club saying "out for six weeks" is information
  FPL's `chance_of_playing_next_round` structurally cannot carry, and for the 47
  players in the pool whose official news reads "Unknown return date" it is the
  only estimate of when they are back that exists anywhere.
- `OUT_FOR_SEASON` carries a duration too, but adds nothing the model can use:
  those players are already at zero and `availability` is not permitted to push
  anyone lower. Recorded because it is worth *reading*.
- `RULED_OUT` and `RETURNING` have no date in them. A recovery curve invented
  from "back in training" would be exactly the improvisation this layer exists
  to avoid, so they are stored, displayed, and never consumed.

Durations are read at their *upper* bound — "two to three weeks" is three — so
the estimate errs late. Being early about a return is the expensive mistake:
it puts a player in the squad who is not going to play.
"""

import datetime as dt
import re
from dataclasses import dataclass

from fplquant.news.parse import resolve_partial_date

RETURN_DATE = "return_date"
OUT_FOR_SEASON = "out_for_season"
RULED_OUT = "ruled_out"
RETURNING = "returning"
NONE = "none"

# Longest absence a duration phrase may express and still be believed. Beyond
# this the sentence is almost always about a contract or a manager's tenure
# rather than an injury, and a return date a year out is no use anyway.
MAX_ABSENCE_DAYS = 300

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "several": 3, "a couple of": 2, "couple": 2, "few": 3,
}  # fmt: skip

_QUANTITY = r"(?:\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
# The separator before the unit allows a hyphen, so "a six-week absence" reads
# the same as "six weeks out" — publishers use both, often in the same article.
_SPAN = (
    rf"(?:(?P<low>{_QUANTITY})\s*(?:to|-|–|or)\s*)?"
    rf"(?P<high>{_QUANTITY})[\s-]+(?P<unit>day|week|month)s?"
)

# An absence cue on the left, then the span: "out for six weeks", "sidelined
# for up to three months", "will miss around two weeks".
_DURATION_AFTER_CUE = re.compile(
    r"\b(?:out|sidelined|absent|unavailable|injured|miss(?:es|ed|ing)?|ruled out|recovery)\b"
    r"[^.!?]{0,40}?\bfor\s+(?:around|about|up\s+to|at\s+least|another)?\s*" + _SPAN,
    re.I,
)
# ...or the span first: "a six-week absence", "three months on the sidelines".
_DURATION_BEFORE_CUE = re.compile(
    _SPAN + r"[^.!?]{0,20}?\b(?:out|on the sidelines|absence|lay[- ]?off|spell out)\b",
    re.I,
)

# "back on 14 September", "returns on 3 Oct", "available from 14 September".
_EXPLICIT_DATE = re.compile(
    r"\b(?:back|return(?:s|ing)?|available)\b[^.!?]{0,25}?\b(?:on|from|after)\s+"
    r"(?P<date>\d{1,2}\s+[A-Za-z]{3,9})",
    re.I,
)
# ...and the American ordering a wire service may use: "back on September 14".
_EXPLICIT_DATE_MONTH_FIRST = re.compile(
    r"\b(?:back|return(?:s|ing)?|available)\b[^.!?]{0,25}?\b(?:on|from|after)\s+"
    r"(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\b",
    re.I,
)

_OUT_FOR_SEASON = re.compile(
    r"\b(?:out|sidelined|miss(?:es|ed|ing)?|ruled out)\b[^.!?]{0,30}?"
    r"\b(?:for the (?:rest of the )?season|for the campaign)\b",
    re.I,
)
_RULED_OUT = re.compile(
    r"\b(?:ruled out|will miss|set to miss|out of (?:the|this) (?:match|game|weekend)"
    r"|faces? a spell|suffered? (?:a|an) \w+ injury)\b",
    re.I,
)
_RETURNING = re.compile(
    r"\b(?:back in (?:full )?training|returned to training|in contention"
    r"|passed a fitness test|available again|set to return|nearing a return"
    r"|closing in on a return|back in the squad)\b",
    re.I,
)


@dataclass(frozen=True)
class ReturnSignal:
    kind: str
    return_date: dt.date | None
    evidence: str  # the sentence it was read from, for the audit trail

    @property
    def feeds_the_model(self) -> bool:
        """Whether this may reach a projection at all. Only a dated return may."""
        return self.kind == RETURN_DATE and self.return_date is not None


def _quantity(word: str) -> int | None:
    word = word.strip().lower()
    if word.isdigit():
        return int(word)
    return _NUMBER_WORDS.get(word)


def _span_days(match: re.Match[str]) -> int | None:
    """A matched duration in days, read at its upper bound.

    The low end of "two to three weeks" is deliberately discarded. A return date
    that arrives early costs nothing — FPL will have published a real status by
    then and this estimate is gone — where one that arrives late puts an absent
    player in the squad.
    """
    high = _quantity(match.group("high"))
    if high is None:
        return None
    unit = _UNIT_DAYS.get(match.group("unit").lower())
    if unit is None:
        return None
    days = high * unit
    return days if 0 < days <= MAX_ABSENCE_DAYS else None


def _sentence_around(text: str, match: re.Match[str]) -> str:
    start = max(text.rfind(".", 0, match.start()) + 1, 0)
    end = text.find(".", match.end())
    return text[start : end if end != -1 else len(text)].strip()[:512]


def extract_signal(text: str, as_of: dt.date) -> ReturnSignal:
    """What this article says about availability, if anything.

    Order matters. "Out for the season" also matches the generic ruled-out
    phrasing and would be the weaker reading of the two, so it is tested first;
    a dated return beats an undated "set to return" for the same reason.
    """
    text = " ".join(text.split())
    if not text:
        return ReturnSignal(NONE, None, "")

    season = _OUT_FOR_SEASON.search(text)
    if season:
        return ReturnSignal(OUT_FOR_SEASON, None, _sentence_around(text, season))

    for pattern in (_EXPLICIT_DATE, _EXPLICIT_DATE_MONTH_FIRST):
        found = pattern.search(text)
        if found is None:
            continue
        groups = found.groupdict()
        raw = groups.get("date") or f"{groups.get('day')} {groups.get('month')}"
        date = resolve_partial_date(raw, as_of)
        if date is not None and as_of <= date <= as_of + dt.timedelta(days=MAX_ABSENCE_DAYS):
            return ReturnSignal(RETURN_DATE, date, _sentence_around(text, found))

    for pattern in (_DURATION_AFTER_CUE, _DURATION_BEFORE_CUE):
        found = pattern.search(text)
        if found is None:
            continue
        days = _span_days(found)
        if days is not None:
            return ReturnSignal(
                RETURN_DATE, as_of + dt.timedelta(days=days), _sentence_around(text, found)
            )

    returning = _RETURNING.search(text)
    if returning:
        return ReturnSignal(RETURNING, None, _sentence_around(text, returning))

    ruled_out = _RULED_OUT.search(text)
    if ruled_out:
        return ReturnSignal(RULED_OUT, None, _sentence_around(text, ruled_out))

    return ReturnSignal(NONE, None, "")
