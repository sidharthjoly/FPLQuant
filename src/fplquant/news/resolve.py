"""Deciding which footballer a news item is about.

This is the dangerous half of reading external news, and it is worth being
explicit about why. Availability is a *hard gate* on expected points. A wrong
match does not add noise the way a wrong injury-history row does — it attaches
one player's fitness bulletin to another player's projection. The existing
Transfermarkt matcher can afford to guess a little because a bad match only
poisons a risk score; this cannot.

So the rules are deliberately strict, and they refuse rather than guess:

- **A full name is enough.** "Bruno Fernandes" in a headline is about Bruno
  Fernandes. Two tokens matching a real player's real name is not a coincidence
  a football feed produces by accident.
- **A surname alone is not**, ever. It has to be corroborated by the club
  appearing in the same text. There are two Silvas and three Joneses in a
  Premier League season, and "Jones ruled out" with no club attached is not
  evidence about any of them. This rule was not a precaution — run over a day
  of live feeds without it, a bare-surname tier matched "Old Trafford" to Leeds'
  goalkeeper James Trafford and "Jacob Greaves" to Hull's Matty Jacob.
- **Whole words only.** Substring matching turns "Sarr" into "Sarri" and "Ward"
  into "Edwards", and both would be silent.
- **A handful of names never resolve without their full form**, because they
  collide with ordinary English or with a stadium. The club check does not save
  those: an Old Trafford match report naturally names both clubs.

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

# Names that collide with ordinary English or with a well-known non-player, and
# therefore only ever resolve as part of a full name. These players are not
# excluded — Rice and Mount matter — but "Rice" in a food headline and "Old
# Trafford" in every Manchester match report are not about them, and the club
# check cannot tell the difference because those articles name the club anyway.
# Empirical: found by running the resolver over a day of live feeds, which is
# also how any addition should be justified.
FULL_NAME_ONLY_ALIASES = frozenset(
    {
        "bailey", "bell", "best", "brown", "cash", "chalk", "cook", "field",
        "fry", "gray", "green", "grey", "hall", "hunter", "jacob", "king",
        "long", "marsh", "may", "moore", "mount", "park", "price", "reed",
        "rice", "sharp", "short", "small", "stone", "trafford", "walker",
        "ward", "white", "wood", "young",
    }
)  # fmt: skip


@dataclass(frozen=True)
class PlayerMatch:
    player_id: int
    confidence: float
    matched_alias: str
    basis: str  # full_name | alias_and_club


@dataclass(frozen=True)
class _Alias:
    text: str  # normalized
    player_id: int
    team_id: int
    is_multi_token: bool


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

        for player in players:
            self._club_terms.setdefault(player.team_id, set()).update(
                {normalize_text(player.team.name), normalize_text(player.team.short_name)}
            )
            for raw in self._alias_forms(player):
                text = _clean_alias(raw)
                if len(text) < MIN_ALIAS_LENGTH:
                    continue
                entry = _Alias(
                    text=text,
                    player_id=player.id,
                    team_id=player.team_id,
                    is_multi_token=" " in text,
                )
                bucket = self._aliases.setdefault(text, [])
                if not any(a.player_id == entry.player_id for a in bucket):
                    bucket.append(entry)

        # Precompiled so the per-article scan is one pass of word-boundary
        # searches rather than a substring sweep — see the module docstring on
        # why substring matching is not an option.
        self._patterns = {
            text: re.compile(rf"(?<!\w){re.escape(text)}(?!\w)") for text in self._aliases
        }

    @staticmethod
    def _alias_forms(player: Player) -> list[str]:
        """Every written form of one player's name worth looking for.

        The last one is not redundant. FPL stores Bruno Fernandes's surname as
        "Borges Fernandes", so neither his full name nor his display name
        ("B.Fernandes") appears in a feed that calls him Bruno Fernandes — which
        every feed does. Pairing the first name with the final token of the
        surname recovers the form the press actually uses, and it is a *longer*
        alias than the surname alone, so it does not weaken anything.
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

    def _club_named(self, text: str, team_id: int) -> bool:
        return any(
            term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text)
            for term in self._club_terms.get(team_id, set())
        )

    def resolve(self, raw_text: str) -> list[PlayerMatch]:
        """Every player this text is plausibly about, best evidence first.

        A player matched on their full name is not also reported on their
        surname: the strongest basis for each player wins, so the confidence
        reflects the best evidence rather than the last rule to fire.
        """
        text = _clean_alias(raw_text)
        best: dict[int, PlayerMatch] = {}

        for alias, entries in self._aliases.items():
            if not self._patterns[alias].search(text):
                continue
            ambiguous = len(entries) > 1
            for entry in entries:
                match = self._score(alias, entry, text, ambiguous)
                if match is None:
                    continue
                current = best.get(entry.player_id)
                if current is None or match.confidence > current.confidence:
                    best[entry.player_id] = match

        return sorted(best.values(), key=lambda m: -m.confidence)

    def _score(self, alias: str, entry: _Alias, text: str, ambiguous: bool) -> PlayerMatch | None:
        if entry.is_multi_token and not ambiguous:
            return PlayerMatch(entry.player_id, FULL_NAME_CONFIDENCE, alias, "full_name")
        if alias in FULL_NAME_ONLY_ALIASES:
            return None
        # A single token, or a name two players share. Either way the club has
        # to appear before this is about anybody in particular.
        if self._club_named(text, entry.team_id):
            return PlayerMatch(entry.player_id, ALIAS_AND_CLUB_CONFIDENCE, alias, "alias_and_club")
        return None


def _clean_alias(text: str) -> str:
    """Normalized, with punctuation flattened to spaces.

    FPL writes abbreviated display names — "Bruno G.", "F.Kadıoğlu" — and a feed
    writes them out in full. Flattening the punctuation makes "F.Kadıoğlu" and
    "F Kadioglu" the same string, and stops a trailing period gluing itself to a
    surname at the end of a headline.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", normalize_text(text))).strip()
