"""The daily news scrape: fetch feeds, resolve players, store what was said.

Run by `fplquant-ingest-news`. Deliberately separate from the reading side —
nothing that serves a request ever touches the network, so a publisher having a
slow morning cannot become a slow endpoint, and a projection can always be
produced from what is already in the database.

Three things are stored: the article, the players it is about, and what it says
about their availability. Keeping them apart is what makes a bad rule findable
later — a mention records *which* alias matched and *why* the resolver believed
it, so a wrong match is a query rather than a re-derivation.

This job fails loudly when it produces nothing. That is not defensive
programming, it is this project's own history: the injury workflow reported
success every week for a month while writing an empty table, because nothing
ever asked whether it had. A scrape that resolves nobody is a failure however
cleanly the process exits.
"""

import argparse
import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from fplquant.config import settings
from fplquant.models.base import session_scope
from fplquant.models.orm import NewsArticle, NewsMention
from fplquant.news.extract import RETURN_DATE, extract_signal
from fplquant.news.feeds import FeedClient, FeedItem
from fplquant.news.resolve import PlayerIndex

logger = logging.getLogger(__name__)


@dataclass
class IngestReport:
    """What one run actually did — the numbers the job's health check reads."""

    fetched: int = 0
    stored: int = 0  # articles new to the database
    skipped_old: int = 0
    mentions: int = 0
    dated_returns: int = 0  # mentions carrying a date, the only kind the model reads

    def __str__(self) -> str:
        return (
            f"fetched={self.fetched} new_articles={self.stored} too_old={self.skipped_old} "
            f"mentions={self.mentions} dated_returns={self.dated_returns}"
        )


class EmptyScrapeError(RuntimeError):
    """Raised when a run fetched nothing at all. See the module docstring."""


def _too_old(item: FeedItem, cutoff: dt.datetime) -> bool:
    """Whether this item is too old to say anything about who is fit now.

    An item with no timestamp is *not* treated as old. Feeds that omit dates
    put them on every item, so discarding them would silently drop a whole
    publisher; the age check that matters is applied again at read time, where
    a missing date sorts last rather than passing.
    """
    if item.published_at is None:
        return False
    return item.published_at < cutoff


def ingest_news(
    session: Session,
    client: FeedClient | None = None,
    as_of: dt.datetime | None = None,
) -> IngestReport:
    """Fetch every configured feed and store what it says about the player pool."""
    as_of = as_of or dt.datetime.now(dt.UTC)
    today = as_of.date()
    cutoff = as_of - dt.timedelta(days=settings.news_article_max_age_days)
    report = IngestReport()

    owns_client = client is None
    client = client or FeedClient()
    try:
        items = client.fetch_all()
    finally:
        if owns_client:
            client.close()

    report.fetched = len(items)
    if not items:
        raise EmptyScrapeError(
            f"No items from any of {len(settings.news_feed_url_list)} configured feed(s). "
            "Either every feed is down at once, which is unlikely, or the URLs are wrong "
            "or the host is being blocked. Check FPLQUANT_NEWS_FEED_URLS."
        )

    index = PlayerIndex(session)
    existing = {
        (source, guid) for source, guid in session.query(NewsArticle.source, NewsArticle.guid).all()
    }

    for item in items:
        if _too_old(item, cutoff):
            report.skipped_old += 1
            continue
        if (item.source, item.guid) in existing:
            # Feeds carry the same story for days. Re-reading it is not new
            # evidence, and re-storing it would make a mention look fresher
            # than the reporting behind it.
            continue
        existing.add((item.source, item.guid))

        article = NewsArticle(
            source=item.source,
            guid=item.guid,
            url=item.url,
            title=item.title,
            summary=item.summary,
            published_at=item.published_at,
            fetched_at=as_of,
        )
        session.add(article)
        session.flush()
        report.stored += 1

        signal = extract_signal(item.text, today)
        for match in index.resolve(item.text):
            session.add(
                NewsMention(
                    article_id=article.id,
                    player_id=match.player_id,
                    confidence=match.confidence,
                    matched_alias=match.matched_alias[:128],
                    match_basis=match.basis,
                    signal=signal.kind,
                    return_date=signal.return_date,
                    evidence=signal.evidence,
                )
            )
            report.mentions += 1
            if signal.kind == RETURN_DATE:
                report.dated_returns += 1

    return report


def prune_articles(session: Session, as_of: dt.datetime | None = None) -> int:
    """Delete articles older than the read-time window. Returns rows removed.

    The reading side ignores them already, so this is housekeeping rather than
    correctness — but an unbounded table on a free-tier VM with a SQLite file
    is a slow-motion outage, and a few hundred items a day adds up.
    """
    as_of = as_of or dt.datetime.now(dt.UTC)
    cutoff = as_of - dt.timedelta(days=settings.news_article_max_age_days)
    stale = (
        session.query(NewsArticle)
        .filter(NewsArticle.published_at.isnot(None), NewsArticle.published_at < cutoff)
        .all()
    )
    for article in stale:
        session.delete(article)  # mentions cascade
    return len(stale)


def run_news_ingest(client: FeedClient | None = None, prune: bool = True) -> IngestReport:
    with session_scope() as session:
        report = ingest_news(session, client=client)
        if prune:
            removed = prune_articles(session)
            if removed:
                logger.info("Pruned %d article(s) past the read window", removed)
    logger.info("News ingest: %s", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch football news feeds and index them.")
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep articles older than the read window instead of deleting them.",
    )
    parser.add_argument(
        "--require-mentions",
        action="store_true",
        help=(
            "Exit non-zero unless the run resolved at least one player. Use in CI: a scrape "
            "that fetches fine and matches nobody is broken, and looks identical to a quiet "
            "news day unless something asserts otherwise."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    report = run_news_ingest(prune=not args.no_prune)
    if args.require_mentions and report.mentions == 0:
        raise SystemExit(
            f"News ingest resolved no players ({report}). Either every article was already "
            "stored — check new_articles — or the resolver is matching nothing, which means "
            "the player pool is empty or the feeds have changed shape."
        )


if __name__ == "__main__":
    main()
