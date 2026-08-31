"""Reading public football news feeds.

RSS and Atom rather than page scraping, and the distinction is not cosmetic. A
feed is published *in order to be* syndicated, so it is a contract rather than
markup that changes without notice; it carries a stable per-item id, a
timestamp and a summary, which is exactly the metadata this layer needs and
exactly what has to be reverse-engineered out of HTML; and it does not attract
the bot-blocking that already stops this project's Transfermarkt scrape running
anywhere but a laptop. The client identifies itself honestly and sleeps between
requests.

Both formats are parsed by the same code because they differ only in tag names
and date encoding, and `lxml` — already a dependency — reads either. Nothing
here interprets the text; that is `resolve` and `extract`, kept separate so a
publisher changing its prose cannot break the fetch, and a feed going down
cannot break the parsing tests.
"""

import datetime as dt
import email.utils
import logging
import time
import urllib.parse
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from fplquant.config import settings

logger = logging.getLogger(__name__)

# Tags carrying the same thing under the two formats, most specific first.
# Matched case-insensitively: the XML parser is case-sensitive by spec, and RSS
# spells its timestamp `pubDate` while Atom spells its equivalents lowercase.
# Getting this wrong is silent — every item simply comes back undated, and the
# age filter then discards the lot.
_LINK_TAGS = ("link", "guid", "id")
_SUMMARY_TAGS = ("description", "summary", "content")
_DATE_TAGS = ("pubdate", "published", "updated", "date")


@dataclass(frozen=True)
class FeedItem:
    """One story, as the feed published it."""

    source: str  # the feed's own title, or its host when it doesn't give one
    guid: str  # stable per-item id — the field the formats provide for dedup
    url: str
    title: str
    summary: str
    published_at: dt.datetime | None

    @property
    def text(self) -> str:
        """Title and summary together — everything there is to read.

        Feeds carry a headline and a standfirst, not article bodies. That is a
        real limit and mostly a helpful one: a standfirst that mentions a player
        is usually *about* that player, where an article body mentions a dozen
        people in passing and would resolve half a squad off one story.
        """
        return f"{self.title}. {self.summary}".strip()


def parse_datetime(value: str) -> dt.datetime | None:
    """A feed timestamp in either format's convention, as an aware UTC datetime.

    RSS uses RFC 822 ("Mon, 31 Aug 2026 09:14:00 GMT"), Atom uses ISO 8601.
    Anything unreadable returns None rather than today: an item with an unknown
    date must not be able to pass an age check it hasn't earned.
    """
    value = value.strip()
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _first_text(entry: Tag, names: tuple[str, ...]) -> str:
    children = [child for child in entry.find_all(recursive=False) if isinstance(child, Tag)]
    by_name: dict[str, list[Tag]] = {}
    for child in children:
        by_name.setdefault(child.name.lower(), []).append(child)

    for name in names:
        for found in by_name.get(name, []):
            text = found.get_text(strip=True)
            if text:
                return text
            # Atom puts the link in an attribute rather than in the element.
            href = found.get("href")
            if href:
                return str(href)
    return ""


def parse_feed(xml: str, fallback_source: str) -> list[FeedItem]:
    """Every item in one feed document. Never raises on malformed input.

    A feed that has gone to HTML — an error page served with a 200, which is
    common — simply yields nothing, and the caller logs it. That is the right
    shape: one broken feed must not take the others down with it.
    """
    soup = BeautifulSoup(xml, "xml")
    channel_title = soup.find("title")
    source = (
        channel_title.get_text(strip=True)
        if isinstance(channel_title, Tag) and channel_title.get_text(strip=True)
        else fallback_source
    )

    items: list[FeedItem] = []
    for entry in soup.find_all(["item", "entry"]):
        if not isinstance(entry, Tag):
            continue
        title = _first_text(entry, ("title",))
        url = _first_text(entry, _LINK_TAGS)
        guid = _first_text(entry, ("guid", "id")) or url
        if not title or not guid:
            continue
        # Summaries arrive with markup in them often enough to matter, and a
        # stray tag would otherwise end up inside a matched alias.
        raw_summary = _first_text(entry, _SUMMARY_TAGS)
        summary = BeautifulSoup(raw_summary, "html.parser").get_text(" ", strip=True)

        items.append(
            FeedItem(
                source=source[:64],
                guid=guid[:512],
                url=url[:1024],
                title=title[:512],
                summary=summary[:4096],
                published_at=parse_datetime(_first_text(entry, _DATE_TAGS)),
            )
        )
    return items


class FeedClient:
    """Fetches the configured feeds, politely and one at a time."""

    def __init__(self, urls: list[str] | None = None, delay_seconds: float | None = None) -> None:
        self.urls = urls if urls is not None else settings.news_feed_url_list
        self.delay = (
            delay_seconds if delay_seconds is not None else settings.news_request_delay_seconds
        )
        self.session = requests.Session()
        self.session.headers["User-Agent"] = settings.news_user_agent
        self.session.headers["Accept"] = "application/rss+xml, application/atom+xml, text/xml"

    def fetch_all(self) -> list[FeedItem]:
        """Items from every configured feed, with failures logged and skipped.

        Returning what did work rather than raising is deliberate: this runs on
        a schedule against third-party servers, and one publisher having a bad
        morning is not a reason to discard the other two. The caller checks the
        total — see `fplquant.news.ingest_news`, which fails loudly on zero,
        because a scrape that silently produces nothing is this project's
        established way of being broken for a month without noticing.
        """
        items: list[FeedItem] = []
        for index, url in enumerate(self.urls):
            if index:
                time.sleep(self.delay)
            try:
                items.extend(self.fetch(url))
            except requests.RequestException:
                logger.warning("Feed fetch failed for %s; skipping", url, exc_info=True)
        return items

    def fetch(self, url: str) -> list[FeedItem]:
        response = self.session.get(url, timeout=settings.http_timeout_seconds)
        response.raise_for_status()
        host = urllib.parse.urlparse(url).netloc or url
        items = parse_feed(response.text, fallback_source=host)
        if not items:
            logger.warning("Feed %s parsed to zero items — wrong URL, or an error page?", url)
        return items

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "FeedClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
