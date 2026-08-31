"""Parsing feed documents, without touching the network."""

import datetime as dt
import logging

import pytest
import requests

from fplquant.news.feeds import FeedClient, parse_datetime, parse_feed

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>BBC Sport</title>
  <item>
    <title>Saka out for six weeks</title>
    <description>Arsenal will be &lt;b&gt;without&lt;/b&gt; Bukayo Saka.</description>
    <link>https://example.invalid/saka</link>
    <guid isPermaLink="false">urn:bbc:12345</guid>
    <pubDate>Mon, 31 Aug 2026 09:14:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Feed</title>
  <entry>
    <title>Palmer nearing return</title>
    <summary>Chelsea expect him back soon.</summary>
    <link href="https://example.invalid/palmer"/>
    <id>tag:example.invalid,2026:1</id>
    <published>2026-08-30T18:00:00Z</published>
  </entry>
</feed>"""


def test_an_rss_item_is_read_whole() -> None:
    (item,) = parse_feed(RSS, fallback_source="fallback")
    assert item.source == "BBC Sport"
    assert item.title == "Saka out for six weeks"
    assert item.guid == "urn:bbc:12345"
    assert item.url == "https://example.invalid/saka"
    # Markup in a summary would otherwise end up inside a matched player alias.
    assert item.summary == "Arsenal will be without Bukayo Saka."
    assert item.published_at == dt.datetime(2026, 8, 31, 9, 14, tzinfo=dt.UTC)


def test_an_atom_entry_is_read_by_the_same_code() -> None:
    (item,) = parse_feed(ATOM, fallback_source="fallback")
    assert item.title == "Palmer nearing return"
    # Atom puts the link in an attribute rather than in the element body.
    assert item.url == "https://example.invalid/palmer"
    assert item.published_at == dt.datetime(2026, 8, 30, 18, 0, tzinfo=dt.UTC)


def test_the_rss_timestamp_tag_is_matched_whatever_its_case() -> None:
    """RSS spells it `pubDate` and the XML parser is case-sensitive by spec.

    Getting this wrong is silent: every item comes back undated, and the
    freshness filter then quietly discards the entire feed.
    """
    (item,) = parse_feed(RSS.replace("pubDate", "PUBDATE"), fallback_source="x")
    assert item.published_at is not None


def test_a_feed_that_has_become_an_error_page_yields_nothing() -> None:
    """Publishers serve HTML error pages with a 200 often enough to matter.
    One broken feed must not take the others down with it."""
    assert parse_feed("<html><body>503 Service Unavailable</body></html>", "x") == []
    assert parse_feed("", "x") == []


def test_an_unreadable_timestamp_is_absent_rather_than_now() -> None:
    """An item with an unknown date must not pass a freshness check it hasn't
    earned — which is what defaulting to today would let it do."""
    assert parse_datetime("sometime last week") is None
    assert parse_datetime("") is None


def test_a_naive_timestamp_is_read_as_utc() -> None:
    parsed = parse_datetime("2026-08-30T18:00:00")
    assert parsed == dt.datetime(2026, 8, 30, 18, 0, tzinfo=dt.UTC)


class _Response:
    def __init__(self, text: str, error: Exception | None = None) -> None:
        self.text = text
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error


def test_one_publisher_having_a_bad_morning_does_not_take_the_others_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This runs on a schedule against third-party servers. Returning what did
    work beats discarding two good feeds because a third timed out."""
    client = FeedClient(urls=["https://broken.invalid/f", "https://ok.invalid/f"], delay_seconds=0)

    def fake_get(url: str, **_kwargs: object) -> _Response:
        if "broken" in url:
            return _Response("", requests.ConnectTimeout("down"))
        return _Response(RSS)

    monkeypatch.setattr(client.session, "get", fake_get)

    items = client.fetch_all()

    assert [item.title for item in items] == ["Saka out for six weeks"]


def test_a_feed_url_that_returns_no_items_is_reported_rather_than_hidden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = FeedClient(urls=["https://ok.invalid/f"], delay_seconds=0)
    monkeypatch.setattr(client.session, "get", lambda *a, **k: _Response("<html>oops</html>"))

    with caplog.at_level(logging.WARNING):
        assert client.fetch_all() == []
    assert "zero items" in caplog.text
