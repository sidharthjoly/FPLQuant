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
