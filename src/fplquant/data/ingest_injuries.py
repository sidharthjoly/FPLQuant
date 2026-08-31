import argparse
import logging
import time
import urllib.parse
from collections.abc import Callable

from sqlalchemy.orm import Session

from fplquant.config import settings
from fplquant.data.player_matching import match_player
from fplquant.data.transfermarkt_client import TransfermarktClient
from fplquant.models.base import session_scope
from fplquant.models.orm import InjuryRecord, Player

logger = logging.getLogger(__name__)


def resolve_transfermarkt_id(session: Session, client: TransfermarktClient, player: Player) -> None:
    """Search Transfermarkt for `player` and cache the match (or the lack of one).

    No-op if already resolved (matched or previously confirmed unmatched) —
    call `resolve_transfermarkt_id` only for players whose
    `transfermarkt_lookup_status == "unresolved"` to avoid needless requests.
    """
    full_name = f"{player.first_name} {player.second_name}"
    query = urllib.parse.quote(full_name)
    candidates = client.search_player(query)

    if not candidates:
        # An empty result set is not evidence about this player. Transfermarkt
        # has no public API and does block — a datacentre IP can get an
        # empty-looking page for every query while the same code from a
        # residential connection resolves the whole pool. Caching that as
        # "unmatched" is permanent: the caller only ever revisits players who
        # are still "unresolved", so a single blocked run silently retires the
        # entire squad from ever being looked up again.
        #
        # This is not hypothetical. Production reached 623 unmatched and 0
        # matched — every player in the game — which is not a plausible thing
        # for a database of footballers to be true of, and left the injury
        # model running on age and minutes alone with nothing to indicate it.
        logger.warning(
            "Transfermarkt returned no candidates at all for %s (%s) — leaving unresolved "
            "so a later run retries. Repeated across the pool, this means the search is "
            "being blocked rather than the players being absent.",
            full_name,
            player.team.short_name,
        )
        return

    match = match_player(
        fpl_full_name=full_name,
        fpl_web_name=player.web_name,
        fpl_team_name=player.team.name,
        candidates=candidates,
    )
    if match is None:
        # Candidates came back and none was close enough. That *is* evidence
        # about this player, so it is worth caching.
        player.transfermarkt_lookup_status = "unmatched"
        logger.info("No Transfermarkt match for %s (%s)", full_name, player.team.short_name)
        return
    player.transfermarkt_id = match.transfermarkt_id
    player.transfermarkt_slug = match.slug
    player.transfermarkt_lookup_status = "matched"
    session.flush()


def sync_injury_history(session: Session, client: TransfermarktClient, player: Player) -> None:
    """Replace `player`'s injury records with a fresh scrape from Transfermarkt."""
    if player.transfermarkt_id is None or player.transfermarkt_slug is None:
        return

    records = client.get_injury_history(player.transfermarkt_slug, player.transfermarkt_id)

    session.query(InjuryRecord).filter_by(player_id=player.id).delete()
    for record in records:
        session.add(
            InjuryRecord(
                player_id=player.id,
                season=record.season,
                injury_type=record.injury_type,
                start_date=record.start_date,
                end_date=record.end_date,
                days_out=record.days_out,
                games_missed=record.games_missed,
            )
        )
    session.flush()


def sync_nationality(session: Session, client: TransfermarktClient, player: Player) -> None:
    """Fetch and store `player`'s nationality from their Transfermarkt profile.

    Unlike injury history, nationality doesn't change, so this only needs to
    run once per player — callers should only call it for players where
    `nationality is None`, to avoid re-fetching a page for no reason.
    """
    if player.transfermarkt_id is None or player.transfermarkt_slug is None:
        return

    player.nationality = client.get_nationality(player.transfermarkt_slug, player.transfermarkt_id)
    session.flush()


def clear_unmatched_cache(session: Session) -> int:
    """Put every `unmatched` player back to `unresolved`. Returns how many.

    `unmatched` is a permanent verdict — nothing ever looks at those players
    again — so a run that failed for reasons having nothing to do with the
    players themselves needs a way to be taken back. Without this the only
    remedy is hand-editing the database on the server.
    """
    players = session.query(Player).filter_by(transfermarkt_lookup_status="unmatched").all()
    for player in players:
        player.transfermarkt_lookup_status = "unresolved"
    session.flush()
    return len(players)


# How often to commit mid-pass. Scraping the pool is forty minutes of network
# calls; holding all of it in one transaction means an interruption anywhere
# throws away everything before it too.
_COMMIT_EVERY = 25


def _each(
    session: Session,
    players: list[Player],
    step: Callable[[Session, TransfermarktClient, Player], None],
    client: TransfermarktClient,
    delay: float,
    label: str,
) -> None:
    """Run `step` over every player, surviving individual failures.

    One player must not be able to end the run, and the reason is not
    hypothetical caution. A single squad member is named
    `Rodrigo 'Rodri' Hernandez Cascante`; the apostrophe in the search query
    makes Transfermarkt answer 500, the HTTPError propagated out of the loop,
    and `session_scope` rolled the transaction back — discarding 425 players
    that had already resolved perfectly well. The table stayed empty and the
    only trace was a traceback at the end of a forty-minute job.

    So a failed player is logged and skipped, leaving them `unresolved` so a
    later run picks them up, and progress is committed as it goes rather than
    held hostage to the last request succeeding.
    """
    total = len(players)
    failures = 0
    for i, player in enumerate(players, start=1):
        try:
            step(session, client, player)
        except Exception:
            failures += 1
            logger.warning(
                "%s failed for %s (%s); skipping",
                step.__name__,
                player.web_name,
                player.team.short_name,
                exc_info=True,
            )
        time.sleep(delay)
        if i % _COMMIT_EVERY == 0:
            session.commit()
        if i % 25 == 0 or i == total:
            logger.info("%s %d/%d players", label, i, total)
    if failures:
        logger.warning("%s: %d of %d players failed and were skipped", label, failures, total)


def run_injury_ingest(
    client: TransfermarktClient | None = None,
    limit: int | None = None,
    delay_seconds: float | None = None,
    retry_unmatched: bool = False,
) -> None:
    """Resolve Transfermarkt IDs for unresolved players, then sync injury history.

    Rate-limited (one request-pair per player, `delay_seconds` apart) to stay
    polite to Transfermarkt. Given the request volume for a full player pool,
    this is meant to run far less often than the main FPL ingest — see
    .github/workflows/ingest_injuries.yml (weekly, not daily).

    `retry_unmatched` clears the cached "no match" verdicts first, for
    recovering from a run that failed for reasons unrelated to the players.
    """
    owns_client = client is None
    client = client or TransfermarktClient()
    delay = (
        delay_seconds if delay_seconds is not None else settings.transfermarkt_request_delay_seconds
    )
    try:
        if retry_unmatched:
            with session_scope() as session:
                cleared = clear_unmatched_cache(session)
                logger.info("Cleared %d cached 'unmatched' verdicts for retry", cleared)

        with session_scope() as session:
            players = session.query(Player).filter_by(transfermarkt_lookup_status="unresolved")
            if limit is not None:
                players = players.limit(limit)
            unresolved = players.all()
            _each(session, unresolved, resolve_transfermarkt_id, client, delay, "Resolved")

        with session_scope() as session:
            matched = session.query(Player).filter_by(transfermarkt_lookup_status="matched").all()
            _each(session, matched, sync_injury_history, client, delay, "Synced injury history for")

        with session_scope() as session:
            needs_nationality = (
                session.query(Player)
                .filter_by(transfermarkt_lookup_status="matched", nationality=None)
                .all()
            )
            _each(
                session,
                needs_nationality,
                sync_nationality,
                client,
                delay,
                "Fetched nationality for",
            )
    finally:
        if owns_client:
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve Transfermarkt matches and sync injury history."
    )
    parser.add_argument(
        "--retry-unmatched",
        action="store_true",
        help=(
            "Clear cached 'no match' verdicts before running. Use after a run that failed "
            "for reasons unrelated to the players — a blocked search caches every player as "
            "unmatched, and nothing revisits them without this."
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="Only resolve this many players.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    run_injury_ingest(limit=args.limit, retry_unmatched=args.retry_unmatched)


if __name__ == "__main__":
    main()
