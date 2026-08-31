"""Deciding which footballer an article is about.

The precision cases here are not hypothetical. Each was produced by running the
resolver over a day of live BBC, Guardian and Sky feeds.
"""

from sqlalchemy.orm import Session

from fplquant.models.orm import Player
from fplquant.news.resolve import (
    ALIAS_AND_CLUB_CONFIDENCE,
    FULL_NAME_CONFIDENCE,
    PlayerIndex,
)
from tests.engine_helpers import make_team


def _player(session: Session, team, fpl_id: int, first: str, second: str, web: str) -> Player:
    player = Player(
        fpl_id=fpl_id,
        team_id=team.id,
        first_name=first,
        second_name=second,
        web_name=web,
        element_type=3,
        now_cost=60,
        status="a",
        ep_next=4.0,
    )
    session.add(player)
    session.flush()
    return player


def test_a_full_name_is_enough_on_its_own(db_session: Session) -> None:
    team = make_team(db_session, fpl_id=1, short_name="ARS")
    saka = _player(db_session, team, 1, "Bukayo", "Saka", "Saka")

    (match,) = PlayerIndex(db_session).resolve("Bukayo Saka limps off at Anfield")

    assert match.player_id == saka.id
    assert match.confidence == FULL_NAME_CONFIDENCE
    assert match.basis == "full_name"


def test_the_press_form_of_a_compound_surname_still_resolves(db_session: Session) -> None:
    """FPL stores Bruno Fernandes as "Borges Fernandes", so neither his full
    name nor his display name ("B.Fernandes") appears in a feed that calls him
    Bruno Fernandes — which every feed does."""
    team = make_team(db_session, fpl_id=1, short_name="MUN")
    bruno = _player(db_session, team, 1, "Bruno", "Borges Fernandes", "B.Fernandes")

    (match,) = PlayerIndex(db_session).resolve("Bruno Fernandes sets the standard")

    assert match.player_id == bruno.id
    assert match.confidence == FULL_NAME_CONFIDENCE


def test_a_surname_alone_resolves_to_nobody(db_session: Session) -> None:
    """The rule that stops one player's fitness bulletin landing on another's
    projection. A surname with no club attached is not evidence about anyone."""
    team = make_team(db_session, fpl_id=1, short_name="ARS")
    _player(db_session, team, 1, "Bukayo", "Saka", "Saka")

    assert PlayerIndex(db_session).resolve("Saka ruled out") == []


def test_a_surname_with_the_club_named_resolves(db_session: Session) -> None:
    team = make_team(db_session, fpl_id=1, short_name="ARS")
    team.name = "Arsenal"
    saka = _player(db_session, team, 1, "Bukayo", "Saka", "Saka")
    db_session.flush()

    (match,) = PlayerIndex(db_session).resolve("Arsenal confirm Saka is ruled out")

    assert match.player_id == saka.id
    assert match.confidence == ALIAS_AND_CLUB_CONFIDENCE
    assert match.basis == "alias_and_club"


def test_a_stadium_is_not_a_footballer(db_session: Session) -> None:
    """Measured on live feeds: without the full-name-only list, "Old Trafford"
    in a Manchester match report matched Leeds' goalkeeper James Trafford — and
    the club check did not save it, because that report names Leeds too."""
    leeds = make_team(db_session, fpl_id=1, short_name="LEE")
    leeds.name = "Leeds"
    _player(db_session, leeds, 1, "James", "Trafford", "Trafford")
    db_session.flush()

    assert PlayerIndex(db_session).resolve("Leeds beaten at Old Trafford") == []
    # ...but the full name still works, so the player is not lost.
    assert PlayerIndex(db_session).resolve("James Trafford saves a penalty")


def test_a_name_two_players_share_needs_the_club_to_separate_them(
    db_session: Session,
) -> None:
    arsenal = make_team(db_session, fpl_id=1, short_name="ARS")
    arsenal.name = "Arsenal"
    spurs = make_team(db_session, fpl_id=2, short_name="TOT")
    spurs.name = "Tottenham"
    gunner = _player(db_session, arsenal, 1, "Danny", "Jones", "Jones")
    _player(db_session, spurs, 2, "Peter", "Jones", "Jones")
    db_session.flush()
    index = PlayerIndex(db_session)

    assert index.resolve("Jones ruled out for a month") == []

    (match,) = index.resolve("Arsenal say Jones is out for a month")
    assert match.player_id == gunner.id


def test_matching_is_on_whole_words_only(db_session: Session) -> None:
    """Substring matching turns "Sarr" into "Sarri", and would do it silently."""
    team = make_team(db_session, fpl_id=1, short_name="CRY")
    team.name = "Crystal Palace"
    _player(db_session, team, 1, "Ismaila", "Sarr", "Sarr")
    db_session.flush()

    assert PlayerIndex(db_session).resolve("Crystal Palace appoint Maurizio Sarri") == []


def test_the_best_evidence_for_a_player_is_the_one_reported(db_session: Session) -> None:
    """A player matched on their full name should not be downgraded because
    their surname also matched somewhere in the same text."""
    team = make_team(db_session, fpl_id=1, short_name="ARS")
    team.name = "Arsenal"
    _player(db_session, team, 1, "Bukayo", "Saka", "Saka")
    db_session.flush()

    (match,) = PlayerIndex(db_session).resolve("Arsenal's Bukayo Saka: Saka speaks")

    assert match.confidence == FULL_NAME_CONFIDENCE


def test_a_name_inside_a_longer_name_does_not_match_separately(
    db_session: Session,
) -> None:
    """Measured on live feeds: "Barcelona close to deal for Arsenal's Gabriel
    Jesus" resolved to Gabriel Jesus *and* to Gabriel Magalhães, whose name is
    sitting in the middle of it. The longer name claims those words."""
    arsenal = make_team(db_session, fpl_id=1, short_name="ARS")
    arsenal.name = "Arsenal"
    magalhaes = _player(db_session, arsenal, 1, "Gabriel", "dos Santos Magalhães", "Gabriel")
    jesus = _player(db_session, arsenal, 2, "Gabriel", "Fernando de Jesus", "G.Jesus")
    db_session.flush()

    matches = PlayerIndex(db_session).resolve("Barcelona close to deal for Arsenal's Gabriel Jesus")

    assert [m.player_id for m in matches] == [jesus.id]
    assert magalhaes.id not in {m.player_id for m in matches}


def test_a_surname_inside_a_full_name_does_not_match_a_different_player(
    db_session: Session,
) -> None:
    palace = make_team(db_session, fpl_id=1, short_name="CRY")
    palace.name = "Crystal Palace"
    spurs = make_team(db_session, fpl_id=2, short_name="TOT")
    spurs.name = "Spurs"
    ismaila = _player(db_session, palace, 1, "Ismaila", "Sarr", "Sarr")
    _player(db_session, spurs, 2, "Pape Matar", "Sarr", "P.M.Sarr")
    db_session.flush()

    matches = PlayerIndex(db_session).resolve("Spurs and Palace watch Ismaila Sarr")

    assert [m.player_id for m in matches] == [ismaila.id]


def test_a_first_name_never_identifies_a_player_on_its_own(db_session: Session) -> None:
    """Every false positive left after span-claiming was a given name: a byline
    ("Jonathan Wilson"), a player outside the pool ("Bradley Barcola"), a first
    name in a list ("Anthony Gordon"). The pool knows its own given names."""
    brentford = make_team(db_session, fpl_id=1, short_name="BRE")
    brentford.name = "Brentford"
    sunderland = make_team(db_session, fpl_id=2, short_name="SUN")
    sunderland.name = "Sunderland"
    # A player whose *surname* is Wilson, and another whose *first* name is.
    _player(db_session, brentford, 1, "Callum", "Wilson", "Wilson")
    _player(db_session, sunderland, 2, "Wilson", "Isidor", "Isidor")
    db_session.flush()
    index = PlayerIndex(db_session)

    assert index.resolve("Brentford are haunted by last summer | Jonathan Wilson") == []
    # The full name still resolves, so the player is not lost.
    assert [m.matched_alias for m in index.resolve("Callum Wilson scores")] == ["callum wilson"]


def test_the_club_is_recognised_by_the_name_the_press_uses(db_session: Session) -> None:
    """FPL stores "Man Utd". Feeds write "Manchester United". Without the
    expansion the club check silently corroborates nothing, and a real match on
    a bare surname is lost — this exact article was being missed."""
    united = make_team(db_session, fpl_id=1, short_name="MUN")
    united.name = "Man Utd"
    rashford = _player(db_session, united, 1, "Marcus", "Rashford", "Rashford")
    db_session.flush()

    (match,) = PlayerIndex(db_session).resolve(
        "Match report: Manchester United 5-2 Ipswich. Rashford return feels convenient."
    )

    assert match.player_id == rashford.id
    assert match.basis == "alias_and_club"


def test_a_club_name_is_not_read_as_a_player(db_session: Session) -> None:
    """ "Aston Villa" is the commonest multi-word phrase in football writing that
    contains a plausible surname."""
    villa_club = make_team(db_session, fpl_id=1, short_name="AVL")
    villa_club.name = "Aston Villa"
    _player(db_session, villa_club, 1, "Fernando", "Villa", "Villa")
    db_session.flush()

    assert PlayerIndex(db_session).resolve("Aston Villa sign a striker") == []
