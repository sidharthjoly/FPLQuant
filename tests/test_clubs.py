"""Whether two sources are naming the same club."""

from fplquant.data.clubs import formal_club_terms, same_club


def test_an_abbreviation_still_finds_the_club_written_out() -> None:
    """The reason the table exists: FPL abbreviates and nobody else does."""
    assert same_club("Manchester United", name="Man Utd", short_name="MUN")
    assert same_club("Nottingham Forest", name="Nott'm Forest", short_name="NFO")
    assert same_club("Tottenham Hotspur", name="Spurs", short_name="TOT")
    assert same_club("AFC Bournemouth", name="Bournemouth", short_name="BOU")
    assert same_club("Brighton & Hove Albion", name="Brighton", short_name="BHA")


def test_a_different_club_that_reads_similar_is_not_the_club() -> None:
    """Scored on string similarity "Man City" reads 0.64 against "Melbourne
    City FC" — above any threshold loose enough to also admit "Manchester
    City". A Costa Rican centre-back at Melbourne City was matched to Rodri on
    exactly that arithmetic, and a wrong match here is cached permanently."""
    assert not same_club("Melbourne City FC", name="Man City", short_name="MCI")
    assert not same_club("Villarreal CF", name="Aston Villa", short_name="AVL")
    assert not same_club("Al-Hilal SFC", name="Arsenal", short_name="ARS")
    assert not same_club("Fluminense Football Club", name="Arsenal", short_name="ARS")


def test_a_club_missing_from_the_table_still_matches_itself() -> None:
    """A promoted side nobody has listed yet degrades to its own name rather
    than matching nothing at all."""
    assert same_club("Luton Town FC", name="Luton", short_name="LUT")
    assert not same_club("Ipswich Town", name="Luton", short_name="LUT")


def test_nicknames_are_dropped_when_reading_another_databases_club_field() -> None:
    """ "The Gunners" belongs in a match report, not in a squad list, so
    admitting it against a club *field* can only manufacture agreement."""
    assert "gunners" not in formal_club_terms("Arsenal", "ARS")
    assert "cherries" not in formal_club_terms("Bournemouth", "BOU")
    # ...while the short forms that are part of the club's own name survive.
    assert "forest" in formal_club_terms("Nott'm Forest", "NFO")
    assert "manchester united" in formal_club_terms("Man Utd", "MUN")
