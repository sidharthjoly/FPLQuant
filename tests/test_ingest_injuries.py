import datetime as dt

import requests
from sqlalchemy.orm import Session

from fplquant.data import ingest_injuries
from fplquant.data.ingest_injuries import (
    resolve_transfermarkt_id,
    sync_injury_history,
    sync_nationality,
)
from fplquant.data.transfermarkt_client import InjuryRecordData, TransfermarktSearchResult
from fplquant.models.orm import InjuryRecord, Player, Team


class StubTransfermarktClient:
    def __init__(
        self,
        search_results: list[TransfermarktSearchResult],
        injury_records: list[InjuryRecordData],
        nationality: str | None = None,
    ) -> None:
        self._search_results = search_results
        self._injury_records = injury_records
        self._nationality = nationality
        self.search_calls: list[str] = []
        self.injury_calls: list[tuple[str, int]] = []
        self.nationality_calls: list[tuple[str, int]] = []

    def search_player(self, name: str) -> list[TransfermarktSearchResult]:
        self.search_calls.append(name)
        return self._search_results

    def get_injury_history(self, slug: str, transfermarkt_id: int) -> list[InjuryRecordData]:
        self.injury_calls.append((slug, transfermarkt_id))
        return self._injury_records

    def get_nationality(self, slug: str, transfermarkt_id: int) -> str | None:
        self.nationality_calls.append((slug, transfermarkt_id))
        return self._nationality


def _team_and_player(session: Session) -> Player:
    team = Team(fpl_id=1, name="Arsenal", short_name="ARS")
    session.add(team)
    session.flush()
    player = Player(
        fpl_id=1,
        team_id=team.id,
        first_name="Bukayo",
        second_name="Saka",
        web_name="Saka",
        element_type=3,
        now_cost=95,
        status="a",
    )
    session.add(player)
    session.flush()
    return player


def test_resolve_transfermarkt_id_stores_match(db_session: Session) -> None:
    player = _team_and_player(db_session)
    client = StubTransfermarktClient(
        search_results=[
            TransfermarktSearchResult(
                transfermarkt_id=433177,
                slug="bukayo-saka",
                name="Bukayo Saka",
                club_name="Arsenal FC",
                position="RW",
            )
        ],
        injury_records=[],
    )

    resolve_transfermarkt_id(db_session, client, player)  # type: ignore[arg-type]

    assert player.transfermarkt_id == 433177
    assert player.transfermarkt_slug == "bukayo-saka"
    assert player.transfermarkt_lookup_status == "matched"


def test_resolve_transfermarkt_id_marks_unmatched_when_no_good_candidate(
    db_session: Session,
) -> None:
    player = _team_and_player(db_session)
    client = StubTransfermarktClient(
        search_results=[
            TransfermarktSearchResult(
                transfermarkt_id=1,
                slug="nobody-similar",
                name="Zzyzx Qwerty",
                club_name="Unrelated FC",
                position="GK",
            )
        ],
        injury_records=[],
    )

    resolve_transfermarkt_id(db_session, client, player)  # type: ignore[arg-type]

    assert player.transfermarkt_id is None
    assert player.transfermarkt_lookup_status == "unmatched"


def test_sync_injury_history_replaces_records(db_session: Session) -> None:
    player = _team_and_player(db_session)
    player.transfermarkt_id = 433177
    player.transfermarkt_slug = "bukayo-saka"
    db_session.flush()

    # Seed a stale record that should be wiped on sync.
    db_session.add(InjuryRecord(player_id=player.id, season="20/21", injury_type="Stale"))
    db_session.flush()

    client = StubTransfermarktClient(
        search_results=[],
        injury_records=[
            InjuryRecordData(
                season="25/26",
                injury_type="Hamstring injury",
                start_date=dt.date(2025, 8, 23),
                end_date=dt.date(2025, 9, 17),
                days_out=26,
                games_missed=5,
            )
        ],
    )

    sync_injury_history(db_session, client, player)  # type: ignore[arg-type]

    records = db_session.query(InjuryRecord).filter_by(player_id=player.id).all()
    assert len(records) == 1
    assert records[0].injury_type == "Hamstring injury"
    assert client.injury_calls == [("bukayo-saka", 433177)]


def test_sync_injury_history_noop_when_unresolved(db_session: Session) -> None:
    player = _team_and_player(db_session)
    client = StubTransfermarktClient(search_results=[], injury_records=[])

    sync_injury_history(db_session, client, player)  # type: ignore[arg-type]

    assert client.injury_calls == []


def test_sync_nationality_stores_result(db_session: Session) -> None:
    player = _team_and_player(db_session)
    player.transfermarkt_id = 433177
    player.transfermarkt_slug = "bukayo-saka"
    db_session.flush()

    client = StubTransfermarktClient(search_results=[], injury_records=[], nationality="England")

    sync_nationality(db_session, client, player)  # type: ignore[arg-type]

    assert player.nationality == "England"
    assert client.nationality_calls == [("bukayo-saka", 433177)]


def test_sync_nationality_noop_when_unresolved(db_session: Session) -> None:
    player = _team_and_player(db_session)
    client = StubTransfermarktClient(search_results=[], injury_records=[], nationality="England")

    sync_nationality(db_session, client, player)  # type: ignore[arg-type]

    assert player.nationality is None
    assert client.nationality_calls == []


def test_an_empty_search_leaves_the_player_unresolved(db_session: Session) -> None:
    """The bug that emptied production's injury table for good.

    An empty result set says nothing about the player — Transfermarkt has no
    public API and does block, and a blocked search looks exactly like a player
    who is not listed. Caching it as `unmatched` is permanent, because the
    driver only ever revisits players who are still `unresolved`. One blocked
    run therefore retired the entire pool from ever being looked up again:
    production reached 623 unmatched and 0 matched, which is not a plausible
    thing to be true of a database of professional footballers.
    """
    team = Team(fpl_id=1, name="Arsenal", short_name="ARS")
    db_session.add(team)
    db_session.flush()
    player = Player(
        fpl_id=1,
        team_id=team.id,
        first_name="Bukayo",
        second_name="Saka",
        web_name="Saka",
        element_type=3,
        now_cost=100,
    )
    db_session.add(player)
    db_session.flush()

    ingest_injuries.resolve_transfermarkt_id(db_session, _BlockedClient(), player)

    assert player.transfermarkt_lookup_status == "unresolved"


def test_a_real_miss_is_still_cached_as_unmatched(db_session: Session) -> None:
    """Candidates came back and none was close enough. That *is* evidence about
    the player, and re-asking every week would be pure waste."""
    team = Team(fpl_id=2, name="Arsenal", short_name="ARS")
    db_session.add(team)
    db_session.flush()
    player = Player(
        fpl_id=2,
        team_id=team.id,
        first_name="Nobody",
        second_name="Atall",
        web_name="Atall",
        element_type=3,
        now_cost=40,
    )
    db_session.add(player)
    db_session.flush()

    ingest_injuries.resolve_transfermarkt_id(db_session, _WrongPlayerClient(), player)

    assert player.transfermarkt_lookup_status == "unmatched"


def test_clearing_the_cache_makes_unmatched_players_retryable(db_session: Session) -> None:
    team = Team(fpl_id=3, name="Arsenal", short_name="ARS")
    db_session.add(team)
    db_session.flush()
    for i in range(3):
        db_session.add(
            Player(
                fpl_id=10 + i,
                team_id=team.id,
                first_name=f"P{i}",
                second_name="X",
                web_name=f"P{i}",
                element_type=3,
                now_cost=40,
                transfermarkt_lookup_status="unmatched",
            )
        )
    db_session.add(
        Player(
            fpl_id=20,
            team_id=team.id,
            first_name="Kept",
            second_name="Match",
            web_name="Kept",
            element_type=3,
            now_cost=40,
            transfermarkt_lookup_status="matched",
        )
    )
    db_session.flush()

    cleared = ingest_injuries.clear_unmatched_cache(db_session)

    assert cleared == 3
    statuses = {p.web_name: p.transfermarkt_lookup_status for p in db_session.query(Player).all()}
    assert all(statuses[f"P{i}"] == "unresolved" for i in range(3))
    assert statuses["Kept"] == "matched"  # a real match is not thrown away


class _BlockedClient:
    """Transfermarkt returning nothing at all — what a blocked IP looks like."""

    def search_player(self, query: str) -> list[TransfermarktSearchResult]:
        return []


class _WrongPlayerClient:
    """A search that works and simply has nobody resembling the query."""

    def search_player(self, query: str) -> list[TransfermarktSearchResult]:
        return [
            TransfermarktSearchResult(
                transfermarkt_id=1,
                slug="someone-else",
                name="Zlatan Ibrahimovic",
                club_name="AC Milan",
                position="CF",
            )
        ]


class _ExplodingClient:
    """Transfermarkt answering 500 for one particular query.

    Not invented for the test: a squad member is named
    `Rodrigo 'Rodri' Hernandez Cascante`, and the apostrophe in the search URL
    really does make the site return an Internal Server Error.
    """

    def __init__(self, blows_up_on: str) -> None:
        self.blows_up_on = blows_up_on
        self.seen: list[str] = []

    def search_player(self, query: str) -> list[TransfermarktSearchResult]:
        self.seen.append(query)
        if self.blows_up_on in query:
            raise requests.HTTPError("500 Server Error")
        return [
            TransfermarktSearchResult(
                transfermarkt_id=hash(query) % 100000,
                slug="someone",
                name=query.replace("%20", " "),
                club_name="Arsenal",
                position="CM",
            )
        ]


def test_one_players_failure_does_not_discard_the_whole_run(db_session: Session) -> None:
    """A forty-minute scrape held in one transaction loses everything to a
    single bad request. It did: an unhandled 500 on one name rolled back 425
    players that had already resolved, leaving the table empty and nothing but
    a traceback at the end of the job to say why."""
    team = Team(fpl_id=1, name="Arsenal", short_name="ARS")
    db_session.add(team)
    db_session.flush()
    names = ["Alpha", "Rodri", "Gamma", "Delta"]
    for i, name in enumerate(names):
        db_session.add(
            Player(
                fpl_id=100 + i,
                team_id=team.id,
                first_name=name,
                second_name="Player",
                web_name=name,
                element_type=3,
                now_cost=50,
            )
        )
    db_session.flush()

    players = db_session.query(Player).order_by(Player.fpl_id).all()
    client = _ExplodingClient(blows_up_on="Rodri")

    ingest_injuries._each(
        db_session, players, ingest_injuries.resolve_transfermarkt_id, client, 0.0, "Resolved"
    )

    statuses = {p.web_name: p.transfermarkt_lookup_status for p in players}
    # Everyone was attempted, not just those before the failure.
    assert len(client.seen) == 4
    # The three good ones survived; the failure is left retryable, not cached.
    assert statuses["Alpha"] == "matched"
    assert statuses["Gamma"] == "matched"
    assert statuses["Delta"] == "matched"
    assert statuses["Rodri"] == "unresolved"


def test_a_run_that_resolved_nobody_is_reported_as_blocked() -> None:
    """Transfermarkt answers every query with an empty result set when it
    blocks a host, so a blocked scrape and a successful one both exit cleanly
    having written nothing. The only thing that tells them apart is whether
    anything moved."""
    blocked = ingest_injuries.InjuryIngestResult(
        attempted=623, resolved=0, synced=0, matched=0, injury_records=0
    )
    assert blocked.looks_blocked


def test_a_full_table_does_not_hide_a_blocked_run() -> None:
    """This is the production state exactly: 3267 injury rows applied by hand
    weeks ago, and a weekly job resolving nobody on top of them. Asserting the
    table is non-empty passes forever and says nothing about the run — which is
    why it went a month unnoticed."""
    stale = ingest_injuries.InjuryIngestResult(
        attempted=19, resolved=0, synced=573, matched=573, injury_records=3267
    )
    assert stale.looks_blocked


def test_a_week_with_nothing_to_resolve_is_not_a_failure() -> None:
    """Every player already has a verdict, so there is no work and no evidence
    of blocking. That must stay quiet, or the check cries wolf until it is
    switched off."""
    quiet = ingest_injuries.InjuryIngestResult(
        attempted=0, resolved=0, synced=573, matched=573, injury_records=3267
    )
    assert not quiet.looks_blocked


def test_partial_progress_is_progress() -> None:
    """Some names genuinely have no Transfermarkt entry. One verdict reached is
    proof the search itself is working."""
    partial = ingest_injuries.InjuryIngestResult(
        attempted=19, resolved=1, synced=574, matched=574, injury_records=3300
    )
    assert not partial.looks_blocked


def test_an_empty_scrape_does_not_delete_the_history_it_could_not_read(
    db_session: Session,
) -> None:
    """The sync replaces a player's records with what came back. A blocked host
    returns an empty page for every player, which is byte-identical to "this
    player has never been injured" — so trusting it deletes the whole table one
    player at a time, while every delete succeeds and the job reports success.

    Production was two and a half hours from exactly this: 3267 hand-applied
    records, 573 matched players, and a weekly cron on a host Transfermarkt
    blocks."""
    player = _team_and_player(db_session)
    player.transfermarkt_id = 433177
    player.transfermarkt_slug = "bukayo-saka"
    player.transfermarkt_lookup_status = "matched"
    db_session.add(
        InjuryRecord(
            player_id=player.id,
            season="25/26",
            injury_type="Hamstring",
            start_date=dt.date(2026, 8, 1),
            end_date=dt.date(2026, 8, 20),
            days_out=19,
            games_missed=3,
        )
    )
    db_session.flush()

    blocked = StubTransfermarktClient(search_results=[], injury_records=[])
    learned = sync_injury_history(db_session, blocked, player)

    assert learned is False
    # The record it could not read is still there.
    assert db_session.query(InjuryRecord).filter_by(player_id=player.id).count() == 1


def test_a_scrape_that_returns_history_still_replaces_it(db_session: Session) -> None:
    """The guard must not turn the sync into an append-only one: real history
    that came back still replaces what was stored."""
    player = _team_and_player(db_session)
    player.transfermarkt_id = 433177
    player.transfermarkt_slug = "bukayo-saka"
    player.transfermarkt_lookup_status = "matched"
    db_session.add(
        InjuryRecord(
            player_id=player.id,
            season="24/25",
            injury_type="Stale row",
            start_date=dt.date(2025, 1, 1),
            end_date=dt.date(2025, 1, 10),
        )
    )
    db_session.flush()

    fresh = StubTransfermarktClient(
        search_results=[],
        injury_records=[
            InjuryRecordData(
                season="25/26",
                injury_type="Hamstring",
                start_date=dt.date(2026, 8, 1),
                end_date=dt.date(2026, 8, 20),
                days_out=19,
                games_missed=3,
            )
        ],
    )
    learned = sync_injury_history(db_session, fresh, player)

    assert learned is True
    stored = db_session.query(InjuryRecord).filter_by(player_id=player.id).all()
    assert [r.injury_type for r in stored] == ["Hamstring"]


def test_a_pass_that_read_no_history_at_all_is_reported_as_blocked() -> None:
    """Once every player is resolved there is nothing left to search, so the
    resolve pass falls silent. The sync pass is what still runs weekly, and it
    is the one that would quietly return nothing on a blocked host."""
    all_resolved_but_blocked = ingest_injuries.InjuryIngestResult(
        attempted=0, resolved=0, synced=0, matched=573, injury_records=3267
    )
    assert all_resolved_but_blocked.looks_blocked
