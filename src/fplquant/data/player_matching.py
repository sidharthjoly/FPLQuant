"""Finding an FPL player on Transfermarkt: what to search for, and what counts
as a match once the results come back.

Two halves, and they fail in opposite directions. `search_queries` decides what
to type into a search box that has no API contract and matches names
*literally*; `match_player` decides which of the results is the player, and a
wrong answer there is cached permanently. So the queries are generous — several
spellings of the same person, tried in order — and the matching is not.
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from fplquant.data.clubs import same_club
from fplquant.data.transfermarkt_client import TransfermarktSearchResult
from fplquant.utils import normalize_text

NAME_MATCH_THRESHOLD = 0.6
CLUB_MATCH_BONUS = 0.25

# Letters that NFKD does not decompose, because they are letters in their own
# right rather than an accented base — stripping to ASCII deletes them outright
# and turns "Đorđe" into "ore". Spelled out here so a folded query keeps the
# shape of the name a search box can match.
_FOLD_EXCEPTIONS = str.maketrans(
    {
        "Đ": "D", "đ": "d", "Ð": "D", "ð": "d", "Ø": "O", "ø": "o",
        "Ł": "L", "ł": "l", "Æ": "Ae", "æ": "ae", "Œ": "Oe", "œ": "oe",
        "ß": "ss", "Þ": "Th", "þ": "th", "ı": "i", "İ": "I",
    }
)  # fmt: skip

# "B." in "B.Badiashile", "G." in "Bruno G." — FPL abbreviates a display name
# when two players share a surname, and the abbreviation matches nobody. The
# two spellings need separate handling because FPL uses both: a standalone
# token, and a letter glued to the surname it qualifies.
_INITIAL = re.compile(r"^\w\.?$")
_INITIAL_PREFIX = re.compile(r"^\w\.(?=\w)")
# FPL stores Rodri as "Rodrigo 'Rodri' Hernandez Cascante". The quoted part is
# an annotation, not part of any name Transfermarkt indexes.
_QUOTED = re.compile(r"""['"“”‘’]""")


def fold_for_query(name: str) -> str:
    """`name` with its accents removed but its letters intact.

    Distinct from `utils.normalize_text`, which lowercases and is used for
    comparing two strings this program already holds. This one produces a
    string to *send*: Transfermarkt's quick search does not fold accents, so
    "Đorđe Petrović" returns nothing where "Dorde Petrovic" returns the player.
    Comparison is symmetric and can afford to mangle both sides equally; a
    query has only one side, and mangling it loses the player.
    """
    folded = unicodedata.normalize("NFKD", name.translate(_FOLD_EXCEPTIONS))
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(stripped.encode("ascii", "ignore").decode("ascii").split())


@dataclass(frozen=True)
class NameQuery:
    """One search to try, and whether its result needs corroborating.

    A fallback query is a name the player is not registered under — a partial
    surname, a display name, sometimes a bare surname that returns players from
    every league Transfermarkt covers. If the player we want is *absent* from
    those results, a stranger can still clear the name threshold on his own,
    and `resolve_transfermarkt_id` caches that answer forever. So a fallback
    may only ever match a candidate playing for the player's own club.
    """

    text: str
    require_club: bool


def _clean_tokens(name: str) -> list[str]:
    tokens = []
    for token in _QUOTED.sub(" ", name).split():
        if _INITIAL.match(token):
            continue
        token = _INITIAL_PREFIX.sub("", token)
        if token:
            tokens.append(token)
    return tokens


def search_queries(*, first_name: str, second_name: str, web_name: str) -> list[NameQuery]:
    """Every search worth making for one player, best evidence first.

    The full name alone is not enough, and the reason is measured rather than
    assumed. Of the 35 players the pool had never resolved, 27 carried either a
    three-part name or a non-ASCII character, and the quick search matches
    literally: `Gabriel Martinelli Silva` returns nothing where
    `Gabriel Martinelli` returns three players and `Martinelli` returns ten;
    `Đorđe Petrović` returns nothing where `Petrovic` returns ten. The players
    left unresolved are exactly the ones whose registered name nobody writes
    down, so the ladder walks from the registered form towards the form the
    football press uses.

    The first rung is the registered name verbatim, so a player who resolves
    today still resolves today, on the same request, unguarded. Everything
    after it is new.

    Both ends of a compound surname get a rung because both conventions are
    real: FPL stores Bruno Guimarães as "Guimarães Rodriguez Moura" and Bruno
    Fernandes as "Borges Fernandes", so one needs the first token and the other
    the last. Folded spellings follow each rung rather than being tried last,
    because an accent is a smaller difference than a different name.

    Every rung below the registered name needs the club, because every one of
    them is a name the player was not registered under. "Gabriel Silva" reads
    0.70 against "Gabriel Martinelli Silva" all by itself, which is enough to
    return a stranger and cache him forever if the real player happens not to
    be in the results. Only the registered spelling — and the same spelling
    with its accents removed, which is not a different name — is trusted to
    identify somebody on its own, so nothing about today's successful matches
    changes.
    """
    surname_tokens = _clean_tokens(second_name)
    given = " ".join(_clean_tokens(first_name))
    display = " ".join(_clean_tokens(web_name))

    # The registered name goes out exactly as FPL stores it, punctuation and
    # all. Six players in the pool are matched today on a name with an
    # apostrophe in it — Matt O'Riley, Dara O'Shea — and tidying that away
    # would ask after "Matt Riley" instead. The cleaned spelling follows as a
    # fallback rather than replacing it.
    forms: list[str] = [f"{first_name} {second_name}", f"{given} {' '.join(surname_tokens)}"]
    if len(surname_tokens) > 1:
        forms.append(f"{given} {surname_tokens[0]}")
        forms.append(f"{given} {surname_tokens[-1]}")
    if display:
        forms.append(display)
    if surname_tokens:
        forms.append(surname_tokens[-1])

    queries: list[NameQuery] = []
    seen: set[str] = set()
    for rung, form in enumerate(forms):
        for text in (form, fold_for_query(form)):
            text = " ".join(text.split())
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            queries.append(NameQuery(text=text, require_club=rung > 0))
    return queries


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def match_player(
    *,
    fpl_full_name: str,
    fpl_web_name: str,
    fpl_team_name: str,
    candidates: list[TransfermarktSearchResult],
    fpl_team_short_name: str = "",
    require_club: bool = False,
) -> TransfermarktSearchResult | None:
    """Pick the best Transfermarkt search result for an FPL player.

    Scores each candidate on name similarity (against both the player's full
    name and their FPL "web name", since Transfermarkt sometimes lists a
    nickname), with a bonus when the candidate's club also matches the
    player's current FPL team. Returns None if the best score doesn't clear
    `NAME_MATCH_THRESHOLD` — ambiguous or missing matches are skipped rather
    than guessed at, since a wrong match would silently poison injury data.

    With `require_club`, a candidate at another club cannot be returned at all.
    That is for searches too thin to identify anybody on their own — see
    `NameQuery` — where the club is the only thing standing between a surname
    and the wrong footballer. The club comparison is whole-word rather than
    fuzzy for the same reason: see `data.clubs.same_club`.
    """
    best_candidate: TransfermarktSearchResult | None = None
    best_score = 0.0

    for candidate in candidates:
        name_score = max(
            _name_similarity(fpl_full_name, candidate.name),
            _name_similarity(fpl_web_name, candidate.name),
        )
        club_matches = same_club(
            candidate.club_name, name=fpl_team_name, short_name=fpl_team_short_name
        )
        if require_club and not club_matches:
            continue
        score = name_score + (CLUB_MATCH_BONUS if club_matches else 0.0)

        if score > best_score:
            best_score = score
            best_candidate = candidate

    if best_candidate is None or best_score < NAME_MATCH_THRESHOLD:
        return None
    return best_candidate
