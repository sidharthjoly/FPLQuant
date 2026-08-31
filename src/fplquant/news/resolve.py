"""Deciding which footballer a news item is about.

This is the dangerous half of reading external news, and it is worth being
explicit about why. Availability is a *hard gate* on expected points. A wrong
match does not add noise the way a wrong injury-history row does — it attaches
one player's fitness bulletin to another player's projection. The existing
Transfermarkt matcher can afford to guess a little because a bad match only
poisons a risk score; this cannot.

Three rules do the work, and each was derived from watching the resolver fail on
real feeds rather than from imagining how it might.

**Longest match wins, and claims its words.** Text is scanned for the longest
name it contains and those words are then spoken for, so a shorter name sitting
inside a longer one cannot match separately. "Barcelona close to deal for
Arsenal's Gabriel Jesus" is about Gabriel Jesus and is *not* also about Gabriel
Magalhães, even though his name is right there in the middle of it. Known
non-player phrases claim words the same way, which is how "Old Trafford" stops
being a story about Leeds' goalkeeper.

**A first name never identifies a player on its own.** Every false positive left
after span-claiming was a given name: a byline ("Jonathan Wilson"), a player
outside the pool ("Bradley Barcola"), a first name in a list ("Anthony Gordon").
The pool knows its own given names — four players are called Gabriel, two Wilson
— so the rule needs no curation: if a token is somebody's first name, a bare
occurrence of it is far more likely to be part of a full name than a reference
to the one player who happens to wear it as a surname.

**A surname needs its club.** Two Silvas and three Joneses in a season, and
"Jones ruled out" with no club attached is not evidence about any of them. Club
names are expanded first, because a feed writes "Manchester United" where FPL
stores "Man Utd" and an unexpanded check silently fails to corroborate anything.

What survives is scored, not accepted. `confidence` reaches the caller and
`fplquant.config.settings.news_min_mention_confidence` decides what may move a
projection; everything above zero is still stored and shown, because a human
reading a maybe-match is exactly the right consumer for one.

The last line of defence is not here at all. `fplquant.news.availability` floors
every projection at FPL's published number, so even a wrong match cannot rule a
fit player out — the worst it can do is make the model optimistic about someone
FPL has already flagged.
"""

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from fplquant.models.orm import Player
from fplquant.utils import normalize_text

# Confidence by how the match was made. `news_min_mention_confidence` sits
# between them or below both: the default of 0.8 admits either, and raising it
# past 0.9 narrows the model's diet to articles that spell a player's name out
# in full while leaving everything else visible in the API.
FULL_NAME_CONFIDENCE = 0.95
ALIAS_AND_CLUB_CONFIDENCE = 0.85

# Shortest single token that may identify a player at all. Below this, initials
# and short surnames collide with ordinary words far too often to be worth the
# club check.
MIN_ALIAS_LENGTH = 4

# Surnames that are also everyday English words. Unlike the given-name rule
# below, this cannot be derived from the pool — nothing in the database knows
# that "rice" is a food — so it is curated, and deliberately short: names that
# collide with a *stadium* are handled by `NON_PLAYER_PHRASES` instead, and
# names that collide with a first name are handled by the pool itself.
COMMON_WORD_SURNAMES = frozenset(
    {
        "bailey", "bell", "best", "brown", "chalk", "cook", "field", "fry",
        "gray", "green", "grey", "hall", "hunter", "king", "long", "marsh",
        "may", "moore", "mount", "price", "reed", "rice", "sharp", "short",
        "small", "stone", "walker", "ward", "white", "wood", "young",
    }
)  # fmt: skip

# Phrases that are not footballers but contain the name of one. They claim their
# words before any player alias is considered, so the surname inside them never
# comes up for matching. This is the same mechanism that stops a short name
# matching inside a longer one — a stadium is just a name we know isn't a person.
NON_PLAYER_PHRASES = frozenset(
    {
        # Grounds whose names contain a current or plausible surname.
        "old trafford", "villa park", "selhurst park", "goodison park",
        "elland road", "bramall lane", "carrow road", "turf moor",
        "the city ground", "king power stadium", "stadium of light",
        "the hawthorns", "craven cottage", "portman road", "molineux",
        "st james park", "stamford bridge", "london stadium", "anfield",
        "emirates stadium", "etihad stadium", "vitality stadium",
        # Competitions, for the same reason.
        "premier league", "champions league", "europa league", "conference league",
        "fa cup", "carabao cup", "world cup", "match of the day",
    }
)  # fmt: skip

# How the press writes each club, against how FPL stores it. Without this the
# club check is far weaker than it looks: FPL's `name` is "Man Utd" and
# "Nott'm Forest", and a feed saying "Manchester United" corroborates neither.
#
# Keyed on FPL's three-letter short name, which is the most stable identifier
# the payload carries. A club that isn't listed — a promoted side, a future
# rename — simply falls back to its FPL name and short name, so this degrades
# to the old behaviour rather than breaking. Bare "City" and "United" are
# deliberately absent: half the league answers to them.
CLUB_ALIASES: dict[str, tuple[str, ...]] = {
    "ARS": ("arsenal", "gunners"),
    "AVL": ("aston villa", "villa"),
    "BHA": ("brighton", "brighton and hove albion", "seagulls"),
    "BOU": ("bournemouth", "afc bournemouth", "cherries"),
    "BRE": ("brentford", "bees"),
    "BUR": ("burnley", "clarets"),
    "CHE": ("chelsea",),
    "COV": ("coventry", "coventry city", "sky blues"),
    "CRY": ("crystal palace", "palace", "eagles"),
    "EVE": ("everton", "toffees"),
    "FUL": ("fulham", "cottagers"),
    "HUL": ("hull", "hull city", "tigers"),
    "IPS": ("ipswich", "ipswich town", "tractor boys"),
    "LEE": ("leeds", "leeds united"),
    "LEI": ("leicester", "leicester city", "foxes"),
    "LIV": ("liverpool",),
    "MCI": ("man city", "manchester city"),
    "MUN": ("man utd", "man united", "manchester united", "red devils"),
    "NEW": ("newcastle", "newcastle united", "magpies"),
    "NFO": ("nottm forest", "nottingham forest", "forest"),
    "SHU": ("sheffield united", "blades"),
    "SOU": ("southampton", "saints"),
    "SUN": ("sunderland", "black cats"),
    "TOT": ("spurs", "tottenham", "tottenham hotspur"),
    "WHU": ("west ham", "west ham united", "hammers"),
    "WOL": ("wolves", "wolverhampton wanderers"),
}


@dataclass(frozen=True)
class PlayerMatch:
    player_id: int
    confidence: float
    matched_alias: str
    basis: str  # full_name | alias_and_club


@dataclass(frozen=True)
class _Alias:
    player_id: int
    team_id: int


@dataclass(frozen=True)
class _Span:
    """One occurrence of a known phrase, in token positions."""

    start: int
    length: int
    phrase: str

    @property
    def end(self) -> int:
        return self.start + self.length


class PlayerIndex:
    """Every name the current pool answers to, built once per ingest run.

    Held as an object rather than rebuilt per article because a daily run reads
    a few hundred items against six hundred players, and the index is the same
    for all of them.
    """

    def __init__(self, session: Session) -> None:
        players = session.query(Player).options(selectinload(Player.team)).all()

        self._aliases: dict[str, list[_Alias]] = {}
        self._club_terms: dict[int, set[str]] = {}
        # Tokens that are somebody's first name. Derived from the pool rather
        # than curated, which is what makes it keep working as squads change.
        self._given_names: set[str] = set()

        for player in players:
            self._club_terms.setdefault(player.team_id, set()).update(
                _club_terms_for(player.team.name, player.team.short_name)
            )
            first = _clean(player.first_name)
            if " " not in first and len(first) >= MIN_ALIAS_LENGTH:
                self._given_names.add(first)

            for raw in _alias_forms(player):
                text = _clean(raw)
                if len(text) < MIN_ALIAS_LENGTH:
                    continue
                bucket = self._aliases.setdefault(text, [])
                if not any(a.player_id == player.id for a in bucket):
                    bucket.append(_Alias(player.id, player.team_id))

        self._blockers = {_clean(phrase) for phrase in NON_PLAYER_PHRASES}
        # Club names block too, so "Aston Villa" is never read as a player
        # called Villa. They are the commonest multi-word phrase in football
        # writing that contains a plausible surname.
        for terms in self._club_terms.values():
            self._blockers.update(term for term in terms if " " in term)

        self._max_tokens = max(
            (len(phrase.split()) for phrase in (*self._aliases, *self._blockers)), default=1
        )

    def resolve(self, raw_text: str) -> list[PlayerMatch]:
        """Every player this text is plausibly about, best evidence first.

        Longest name first, and each claims its words — see the module
        docstring. A player named by their full name is therefore reported once,
        on that basis, rather than a second time on the surname inside it.
        """
        text = _clean(raw_text)
        if not text:
            return []
        tokens = text.split()

        claimed: set[int] = set()
        best: dict[int, PlayerMatch] = {}

        for span in self._spans(tokens):
            if any(position in claimed for position in range(span.start, span.end)):
                continue
            claimed.update(range(span.start, span.end))
            if span.phrase in self._blockers:
                continue  # a stadium or a club: the words are spoken for, nobody matched

            entries = self._aliases.get(span.phrase, [])
            ambiguous = len(entries) > 1
            for entry in entries:
                match = self._score(span, entry, text, ambiguous)
                if match is None:
                    continue
                current = best.get(entry.player_id)
                if current is None or match.confidence > current.confidence:
                    best[entry.player_id] = match

        return sorted(best.values(), key=lambda m: -m.confidence)

    def _spans(self, tokens: list[str]) -> list[_Span]:
        """Known phrases found in `tokens`, longest first then left to right.

        Scanned as n-grams against a dictionary rather than as a few thousand
        regex searches, which is both far faster over a day of articles and
        exactly what makes matching whole-word for free: a token is a word, so
        "Sarr" can never be found inside "Sarri".
        """
        found: list[_Span] = []
        for start in range(len(tokens)):
            for length in range(min(self._max_tokens, len(tokens) - start), 0, -1):
                phrase = " ".join(tokens[start : start + length])
                if phrase in self._aliases or phrase in self._blockers:
                    found.append(_Span(start, length, phrase))
        return sorted(found, key=lambda s: (-s.length, s.start))

    def _club_named(self, text: str, team_id: int) -> bool:
        return any(
            term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text)
            for term in self._club_terms.get(team_id, set())
        )

    def _score(self, span: _Span, entry: _Alias, text: str, ambiguous: bool) -> PlayerMatch | None:
        if span.length > 1 and not ambiguous:
            return PlayerMatch(entry.player_id, FULL_NAME_CONFIDENCE, span.phrase, "full_name")
        # A single token, or a name two players share.
        if span.length == 1 and (
            span.phrase in COMMON_WORD_SURNAMES or span.phrase in self._given_names
        ):
            # An everyday word, or somebody's first name. Either way a bare
            # occurrence is not a reference to this player — and if it were part
            # of a full name, the longer span would already have claimed it.
            return None
        if self._club_named(text, entry.team_id):
            return PlayerMatch(
                entry.player_id, ALIAS_AND_CLUB_CONFIDENCE, span.phrase, "alias_and_club"
            )
        return None


def _alias_forms(player: Player) -> list[str]:
    """Every written form of one player's name worth looking for.

    The last two are not redundant. FPL stores Bruno Fernandes's surname as
    "Borges Fernandes", so neither his full name nor his display name
    ("B.Fernandes") appears in a feed that calls him Bruno Fernandes — which
    every feed does. Pairing the first name with the final token of the surname
    recovers the form the press actually uses.
    """
    surname_tokens = player.second_name.split()
    forms = [
        f"{player.first_name} {player.second_name}",
        player.web_name,
        player.second_name,
    ]
    if len(surname_tokens) > 1:
        forms.append(f"{player.first_name} {surname_tokens[-1]}")
        forms.append(surname_tokens[-1])
    return forms


def _club_terms_for(name: str, short_name: str) -> set[str]:
    """Every way the press might name this club.

    FPL's own name and short name always count, so a club missing from
    `CLUB_ALIASES` still corroborates its own players and this degrades rather
    than breaks.
    """
    terms = {_clean(name), _clean(short_name)}
    extra = CLUB_ALIASES.get(short_name.upper(), ())
    terms.update(_clean(alias) for alias in extra)
    return {term for term in terms if term}


def _clean(text: str) -> str:
    """Normalized, with punctuation flattened to spaces.

    FPL writes abbreviated display names — "Bruno G.", "F.Kadıoğlu" — and a feed
    writes them out in full. Flattening the punctuation makes "F.Kadıoğlu" and
    "F Kadioglu" the same string, and stops a trailing period gluing itself to a
    surname at the end of a headline.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", normalize_text(text))).strip()
