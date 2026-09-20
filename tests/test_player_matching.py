from fplquant.data.player_matching import fold_for_query, match_player, search_queries
from fplquant.data.transfermarkt_client import TransfermarktSearchResult


def test_matches_exact_name_and_club() -> None:
    candidates = [
        TransfermarktSearchResult(
            transfermarkt_id=433177,
            slug="bukayo-saka",
            name="Bukayo Saka",
            club_name="Arsenal FC",
            position="RW",
        ),
    ]
    result = match_player(
        fpl_full_name="Bukayo Saka",
        fpl_web_name="Saka",
        fpl_team_name="Arsenal",
        candidates=candidates,
    )
    assert result is not None
    assert result.transfermarkt_id == 433177


def test_prefers_club_matching_candidate_among_same_name() -> None:
    # Two players who could plausibly share a surname; club should disambiguate.
    candidates = [
        TransfermarktSearchResult(
            transfermarkt_id=1,
            slug="james-wrong-club",
            name="James Smith",
            club_name="Some Other FC",
            position="CB",
        ),
        TransfermarktSearchResult(
            transfermarkt_id=2,
            slug="james-right-club",
            name="James Smith",
            club_name="Arsenal FC",
            position="CB",
        ),
    ]
    result = match_player(
        fpl_full_name="James Smith",
        fpl_web_name="J.Smith",
        fpl_team_name="Arsenal",
        candidates=candidates,
    )
    assert result is not None
    assert result.transfermarkt_id == 2


def test_returns_none_when_no_candidates() -> None:
    result = match_player(
        fpl_full_name="Nobody Real", fpl_web_name="Nobody", fpl_team_name="Arsenal", candidates=[]
    )
    assert result is None


def test_returns_none_when_best_match_is_too_weak() -> None:
    candidates = [
        TransfermarktSearchResult(
            transfermarkt_id=1,
            slug="totally-different",
            name="Zzyzx Qwerty",
            club_name="Unrelated FC",
            position="GK",
        ),
    ]
    result = match_player(
        fpl_full_name="Bukayo Saka",
        fpl_web_name="Saka",
        fpl_team_name="Arsenal",
        candidates=candidates,
    )
    assert result is None


def test_matches_on_web_name_when_full_name_differs_more() -> None:
    # Transfermarkt sometimes lists a common nickname rather than the full name.
    candidates = [
        TransfermarktSearchResult(
            transfermarkt_id=9,
            slug="gakpo",
            name="Gakpo",
            club_name="Liverpool FC",
            position="LW",
        ),
    ]
    result = match_player(
        fpl_full_name="Cody Mathès Gakpo",
        fpl_web_name="Gakpo",
        fpl_team_name="Liverpool",
        candidates=candidates,
    )
    assert result is not None
    assert result.transfermarkt_id == 9


def test_a_bare_surname_cannot_match_a_player_at_another_club() -> None:
    """The failure the fallback rungs exist to avoid.

    Searching "Silva" returns Silvas from every league Transfermarkt covers. If
    the one we want is not among them, a stranger still clears the name
    threshold on his own — and `resolve_transfermarkt_id` caches that answer
    permanently.
    """
    candidates = [
        TransfermarktSearchResult(
            transfermarkt_id=1,
            slug="another-silva",
            name="Silva",
            club_name="Rio Ave FC",
            position="CM",
        ),
    ]
    kwargs = {
        "fpl_full_name": "António João Pereira de Albuquerque Tavares da Silva",
        "fpl_web_name": "Silva",
        "fpl_team_name": "Bournemouth",
        "fpl_team_short_name": "BOU",
        "candidates": candidates,
    }

    assert match_player(**kwargs) is not None  # the unguarded search would take him
    assert match_player(**kwargs, require_club=True) is None


def test_requiring_the_club_still_matches_the_right_player() -> None:
    candidates = [
        TransfermarktSearchResult(
            transfermarkt_id=1,
            slug="wrong-martinelli",
            name="Martinelli",
            club_name="Fluminense FC",
            position="CF",
        ),
        TransfermarktSearchResult(
            transfermarkt_id=2,
            slug="gabriel-martinelli",
            name="Gabriel Martinelli",
            club_name="Arsenal FC",
            position="LW",
        ),
    ]
    result = match_player(
        fpl_full_name="Gabriel Martinelli Silva",
        fpl_web_name="Martinelli",
        fpl_team_name="Arsenal",
        candidates=candidates,
        require_club=True,
    )
    assert result is not None
    assert result.transfermarkt_id == 2


def test_folding_keeps_letters_ascii_would_delete() -> None:
    """NFKD does not decompose Đ, ø or ł — they are letters, not accented bases —
    so a plain strip-to-ASCII turns "Đorđe" into "ore" and searches for nobody."""
    assert fold_for_query("Đorđe Petrović") == "Dorde Petrovic"
    assert fold_for_query("Gyökeres") == "Gyokeres"
    assert fold_for_query("Ødegaard") == "Odegaard"
    assert fold_for_query("Łukasz") == "Lukasz"


def test_a_plain_name_asks_twice_and_stops() -> None:
    queries = search_queries(first_name="Bukayo", second_name="Saka", web_name="Saka")
    assert [q.text for q in queries] == ["Bukayo Saka", "Saka"]


def test_a_registered_name_nobody_writes_down_reaches_the_press_form() -> None:
    """Measured on Transfermarkt: `Gabriel Martinelli Silva` returns nothing,
    `Gabriel Martinelli` returns three and `Martinelli` returns ten."""
    queries = [
        q.text
        for q in search_queries(
            first_name="Gabriel", second_name="Martinelli Silva", web_name="Martinelli"
        )
    ]
    assert queries[0] == "Gabriel Martinelli Silva"
    assert "Gabriel Martinelli" in queries
    assert "Martinelli" in queries


def test_both_ends_of_a_compound_surname_are_tried() -> None:
    """FPL stores one Bruno as "Guimarães Rodriguez Moura" and the other as
    "Borges Fernandes": the press writes the first token for one and the last
    for the other."""
    guimaraes = [
        q.text
        for q in search_queries(
            first_name="Bruno", second_name="Guimarães Rodriguez Moura", web_name="Bruno G."
        )
    ]
    fernandes = [
        q.text
        for q in search_queries(
            first_name="Bruno", second_name="Borges Fernandes", web_name="B.Fernandes"
        )
    ]
    assert "Bruno Guimarães" in guimaraes
    assert "Bruno Fernandes" in fernandes


def test_the_accented_spelling_is_tried_before_a_different_name() -> None:
    """An accent is a smaller difference than a shortened name, so the folded
    form of a rung follows that rung rather than waiting until the end."""
    queries = [
        q.text
        for q in search_queries(first_name="Đorđe", second_name="Petrović", web_name="Petrović")
    ]
    assert queries[:2] == ["Đorđe Petrović", "Dorde Petrovic"]


def test_only_the_registered_name_may_identify_a_player_alone() -> None:
    queries = search_queries(
        first_name="Gabriel", second_name="Martinelli Silva", web_name="Martinelli"
    )
    assert queries[0].require_club is False
    assert all(q.require_club for q in queries[1:])


def test_initials_and_quoted_nicknames_get_a_rung_without_them() -> None:
    """FPL abbreviates a display name when two players share a surname, and
    annotates Rodri's registered name with the name he plays under. Neither
    spelling matches anything in a search that takes its input literally, so
    each gets a cleaned rung — behind the registered name, never instead of
    it."""
    rodri = [
        q.text
        for q in search_queries(
            first_name="Rodrigo 'Rodri'", second_name="Hernandez Cascante", web_name="Rodrigo"
        )
    ]
    badiashile = [
        q.text
        for q in search_queries(
            first_name="Benoît", second_name="Badiashile Mukinayi", web_name="B.Badiashile"
        )
    ]
    assert rodri[0] == "Rodrigo 'Rodri' Hernandez Cascante"
    assert "Rodrigo Rodri Hernandez Cascante" in rodri
    assert not any(q.startswith("B.") for q in badiashile)
    assert "Benoit Badiashile" in badiashile


def test_the_registered_name_goes_out_exactly_as_fpl_stores_it() -> None:
    """Six players in the pool are matched today on a name with an apostrophe
    in it. Tidying the punctuation away before the first request would ask
    after "Matt Riley" and quietly stop finding them."""
    queries = search_queries(first_name="Matt", second_name="O'Riley", web_name="O'Riley")
    assert queries[0].text == "Matt O'Riley"
    assert queries[0].require_club is False
