import datetime as dt

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
