import argparse
import logging
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fplquant.config import settings
from fplquant.data.player_matching import match_player
from fplquant.data.transfermarkt_client import TransfermarktClient
from fplquant.models.base import session_scope
from fplquant.models.orm import InjuryRecord, Player

logger = logging.getLogger(__name__)


def resolve_transfermarkt_id(session: Session, client: TransfermarktClient, player: Player) -> bool:
    """Search Transfermarkt for `player` and cache the match (or the lack of one).

    No-op if already resolved (matched or previously confirmed unmatched) —
    call `resolve_transfermarkt_id` only for players whose
    `transfermarkt_lookup_status == "unresolved"` to avoid needless requests.

    Returns whether a verdict was reached. False means the search told us
    nothing, which across the pool is the signature of a blocked host.
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
        return False

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
        return True
    player.transfermarkt_id = match.transfermarkt_id
    player.transfermarkt_slug = match.slug
    player.transfermarkt_lookup_status = "matched"
    session.flush()
    return True


def sync_injury_history(session: Session, client: TransfermarktClient, player: Player) -> bool:
    """Replace `player`'s injury records with a fresh scrape from Transfermarkt.

    Returns whether the scrape actually returned history, so a caller can tell
    a working run from one where every request came back empty.
    """
    if player.transfermarkt_id is None or player.transfermarkt_slug is None:
        return False

    records = client.get_injury_history(player.transfermarkt_slug, player.transfermarkt_id)

    if not records:
        # An empty history and a blocked request are the same bytes, and they
        # want opposite handling: one means "this player has no injuries, clear
        # their rows", the other means "we learned nothing, keep what we have".
        # Transfermarkt answers a blocked host with an empty page for *every*
        # player, so trusting empty deletes the whole table one player at a
        # time — and reports success, because every delete worked. The weekly
        # VM cron is exactly that host. Refusing to delete on empty costs only
        # a stale row for a player whose history was genuinely retracted, which
        # is rare and recoverable; the other way round is not.
        logger.warning(
            "No injury history returned for %s (%s) — keeping the %d record(s) already "
            "stored rather than treating an empty response as authoritative.",
            player.web_name,
            player.team.short_name,
            len(player.injury_records),
        )
        return False

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
    return True


def sync_nationality(session: Session, client: TransfermarktClient, player: Player) -> bool:
    """Fetch and store `player`'s nationality from their Transfermarkt profile.

    Unlike injury history, nationality doesn't change, so this only needs to
    run once per player — callers should only call it for players where
    `nationality is None`, to avoid re-fetching a page for no reason.
    """
    if player.transfermarkt_id is None or player.transfermarkt_slug is None:
        return False

    player.nationality = client.get_nationality(player.transfermarkt_slug, player.transfermarkt_id)
    session.flush()
    return player.nationality is not None


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
    step: Callable[[Session, TransfermarktClient, Player], bool],
    client: TransfermarktClient,
    delay: float,
    label: str,
) -> int:
    """Run `step` over every player, surviving individual failures.

    Returns how many calls actually learned something, which is the only way to
    tell a working pass from one the host was blocked for — both complete, and
    both log the same "n/n players".

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
    productive = 0
    for i, player in enumerate(players, start=1):
        try:
            if step(session, client, player):
                productive += 1
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
    return productive


@dataclass(frozen=True)
class InjuryIngestResult:
    """What a run actually achieved, so a caller can tell it apart from a no-op.

    A blocked scrape and a scrape with nothing left to do exit identically —
    cleanly, having written nothing. The difference is visible only here:
    `attempted` players were unresolved going in, and `resolved` of them came
    out with a verdict. Transfermarkt refusing a datacentre IP returns an empty
    result set for every query, so it shows up as attempted > 0, resolved == 0.
    """

    attempted: int  # players that were unresolved when the run started
    resolved: int  # ...and that the run reached a verdict on
    synced: int  # matched players whose history the scrape actually returned
    matched: int  # players with a Transfermarkt id, after the run
    injury_records: int  # rows in the injury table, after the run

    @property
    def looks_blocked(self) -> bool:
        """True when the run had work to do and completed none of it.

        The sync pass is the witness, and the resolve pass only speaks when
        there is no other evidence. That asymmetry is not a preference between
        two equal signals: `attempted > 0, resolved == 0` is a perfectly normal
        healthy outcome, because the players still unresolved are the ones the
        search cannot match at all. It searches `first_name + second_name`, and
        `Gabriel Martinelli Silva` returns nothing where `Gabriel Martinelli`
        returns three; of the 35 outstanding, 27 have a three-word name or
        non-ASCII characters. A week where the only candidates left are those
        reaches a verdict on none of them and is not blocked in the slightest.

        The sync pass cannot be fooled that way. Injury history is cumulative,
        so a matched player's past injuries come back every single week — one
        of the 623 having nothing to report is ordinary, all of them having
        nothing means the host answered nothing, which is exactly what
        Transfermarkt does to an IP it refuses. Hence: judge by the sync pass
        whenever anything is matched, and fall back to the resolve pass only
        before the first player has ever matched, where it is all there is.
        """
        if self.matched > 0:
            return self.synced == 0
        return self.attempted > 0 and self.resolved == 0


def run_injury_ingest(
    client: TransfermarktClient | None = None,
    limit: int | None = None,
    delay_seconds: float | None = None,
    retry_unmatched: bool = False,
) -> InjuryIngestResult:
    """Resolve Transfermarkt IDs for unresolved players, then sync injury history.

    Rate-limited (one request-pair per player, `delay_seconds` apart) to stay
    polite to Transfermarkt. Given the request volume for a full player pool,
    this is meant to run far less often than the main FPL ingest — weekly, from
    a laptop on a home connection, via scripts/scrape_and_ship_injuries.sh.
    There is no workflow and no server cron for it: Transfermarkt blocks
    datacentre IPs, so neither can ever resolve anybody. See DEPLOYMENT.md.

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

        attempted = 0
        synced = 0
        with session_scope() as session:
            players = session.query(Player).filter_by(transfermarkt_lookup_status="unresolved")
            if limit is not None:
                players = players.limit(limit)
            unresolved = players.all()
            attempted = len(unresolved)
            _each(session, unresolved, resolve_transfermarkt_id, client, delay, "Resolved")
            still_unresolved = (
                session.query(Player)
                .filter(
                    Player.id.in_([p.id for p in unresolved]),
                    Player.transfermarkt_lookup_status == "unresolved",
                )
                .count()
                if unresolved
                else 0
            )

        with session_scope() as session:
            matched = session.query(Player).filter_by(transfermarkt_lookup_status="matched").all()
            synced = _each(
                session, matched, sync_injury_history, client, delay, "Synced injury history for"
            )

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
        with session_scope() as session:
            return InjuryIngestResult(
                attempted=attempted,
                resolved=attempted - still_unresolved,
                synced=synced,
                matched=session.query(Player)
                .filter_by(transfermarkt_lookup_status="matched")
                .count(),
                injury_records=session.query(InjuryRecord).count(),
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
    parser.add_argument(
        "--require-progress",
        action="store_true",
        help=(
            "Exit non-zero when nothing came back: no injury history for any matched "
            "player, or — before anything has ever matched — no verdict on any unresolved "
            "one. That is the signature of Transfermarkt blocking the host rather than of "
            "a quiet week, and the two are otherwise indistinguishable: both exit cleanly "
            "having written nothing. Use this anywhere nobody reads the log."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    result = run_injury_ingest(limit=args.limit, retry_unmatched=args.retry_unmatched)
    logger.info(
        "Injury ingest: attempted=%d resolved=%d synced=%d matched=%d injury_records=%d",
        result.attempted,
        result.resolved,
        result.synced,
        result.matched,
        result.injury_records,
    )
    if not args.require_progress:
        return
    # The table being non-empty is not evidence this run did anything — it can
    # hold a snapshot applied by hand months ago, which is exactly the state
    # production sat in. Assert the run moved something, not that the data looks
    # sane. See the note in `resolve_transfermarkt_id` on why blocking is silent.
    if result.looks_blocked:
        logger.error(
            "Nothing came back: reached a verdict on %d of %d unresolved players and got "
            "history for %d of %d matched ones. Transfermarkt returns an empty result set "
            "for every query when it blocks a host, and it blocks datacentre IPs — this "
            "host is almost certainly one. The scrape has to run from a residential "
            "connection; see DEPLOYMENT.md.",
            result.resolved,
            result.attempted,
            result.synced,
            result.matched,
        )
        sys.exit(1)
    if result.matched == 0:
        logger.error(
            "No player is matched to Transfermarkt, so there was nothing to sync and the "
            "injury layer is running on age and minutes alone."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
