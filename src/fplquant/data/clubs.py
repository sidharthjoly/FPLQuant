"""Club names, and deciding when two of them are the same club.

FPL writes "Man Utd", "Nott'm Forest", "Spurs". The football press writes
"Manchester United", "Nottingham Forest", "Tottenham Hotspur". Transfermarkt
writes "Manchester United", "Nottingham Forest", "AFC Bournemouth". Every
consumer of an outside source therefore needs the same expansion, and it lives
here so there is one table rather than three.
"""

import re
import unicodedata

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


def flatten(text: str) -> str:
    """Lowercased, accent-stripped, punctuation flattened to spaces.

    "Nott'm Forest" and "Brighton & Hove Albion" have to survive as word
    sequences, because everything below matches whole words.
    """
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def club_terms(name: str, short_name: str) -> set[str]:
    """Every way the press might name this club.

    FPL's own name and short name always count, so a club missing from
    `CLUB_ALIASES` still corroborates its own players and this degrades rather
    than breaks.
    """
    terms = {flatten(name), flatten(short_name)}
    terms.update(flatten(alias) for alias in CLUB_ALIASES.get(short_name.upper(), ()))
    return {term for term in terms if term}


def formal_club_terms(name: str, short_name: str) -> set[str]:
    """The subset of `club_terms` that names a club *as a database lists it*.

    Nicknames are dropped, because this is for comparing against another
    source's club field rather than against prose. "The Gunners" appears in a
    match report and never in a squad list, and admitting it can only create
    false agreement — "Cherries" would make a Bournemouth player of anybody at
    a club with fruit in its name.

    The rule needs no second list: a one-word alias is kept only when the word
    is already part of FPL's own name for the club, which keeps "Forest" for
    Nott'm Forest and "Villa" for Aston Villa while dropping "Magpies" and
    "Toffees". Multi-word aliases are kept as they are — they are expansions
    ("manchester united"), not nicknames.
    """
    own_words = set(flatten(name).split())
    return {
        term
        for term in club_terms(name, short_name)
        if " " in term or term in own_words or term == flatten(short_name)
    }


def same_club(candidate: str, *, name: str, short_name: str) -> bool:
    """Whether another source's club name refers to this FPL club.

    Whole-word, not fuzzy, and the difference is not academic. Scored on string
    similarity, "Man City" reads 0.64 against "Melbourne City FC" — enough to
    pass any threshold loose enough to also accept "Manchester City", which is
    the one it actually has to accept. A Costa Rican centre-back at Melbourne
    City was matched to Rodri on exactly that arithmetic.
    """
    haystack = flatten(candidate)
    if not haystack:
        return False
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack)
        for term in formal_club_terms(name, short_name)
    )
