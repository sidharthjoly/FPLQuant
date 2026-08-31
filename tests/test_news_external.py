"""What a press report is allowed to do to a projection, and what it is not.

The whole safety design of reading external news lives in the merge rule and in
the gates around it. A feed item is resolved to a player by *name*, so a wrong
match is always possible; what makes that survivable is that the only field it
can touch is one FPL left blank, and the only direction a projection can move
is up. These tests are that guarantee.
"""

import datetime as dt

from sqlalchemy.orm import Session

from fplquant.models.orm import NewsArticle, NewsMention, Player
from fplquant.news.availability import availability_timeline, merge_reported_return
from fplquant.news.extract import RETURN_DATE, RETURNING
from fplquant.news.feeds import FeedItem
from fplquant.news.ingest_news import EmptyScrapeError, ingest_news, prune_articles
from fplquant.news.items import NewsCategory, PlayerNews
from fplquant.news.sources import ExternalNewsSource, FPLPlayerNewsSource
from tests.engine_helpers import make_fixture, make_league

AS_OF = dt.datetime(2026, 8, 31, 12, 0, tzinfo=dt.UTC)


def _official(
    category: NewsCategory, return_date: dt.date | None, availability: float
) -> PlayerNews:
    return PlayerNews(
        player_id=1,
        category=category,
        headline="Knee injury - Unknown return date",
        condition="Knee injury",
        return_date=return_date,
        return_is_certain=category is NewsCategory.SUSPENDED,
        next_round_availability=availability,
        source="fpl",
    )


def _reported(return_date: dt.date | None) -> PlayerNews:
    return PlayerNews(
        player_id=1,
        category=NewsCategory.INJURED,
        headline="Star out for six weeks",
        condition=None,
        return_date=return_date,
        return_is_certain=False,
        next_round_availability=0.0,
        source="feeds:BBC Sport",
    )


def test_a_report_fills_in_the_return_date_fpl_left_blank() -> None:
    merged = merge_reported_return(
        _official(NewsCategory.INJURED, None, 0.0), _reported(dt.date(2026, 9, 14))
    )
    assert merged.return_date == dt.date(2026, 9, 14)
    assert merged.return_date_is_reported
    # Everything else is still FPL's.
    assert merged.category is NewsCategory.INJURED
    assert merged.condition == "Knee injury"
    assert merged.next_round_availability == 0.0


def test_fpls_own_return_date_is_never_overridden_by_a_report_of_it() -> None:
    official = _official(NewsCategory.INJURED, dt.date(2026, 9, 5), 0.0)
    merged = merge_reported_return(official, _reported(dt.date(2026, 11, 1)))
    assert merged == official


def test_a_report_cannot_touch_a_fit_player() -> None:
    """The failure that matters. A misresolved article naming the wrong
    footballer must not be able to rule out somebody who is playing."""
    official = _official(NewsCategory.AVAILABLE, None, 1.0)
    assert merge_reported_return(official, _reported(dt.date(2026, 9, 14))) == official


def test_a_report_cannot_touch_a_suspension() -> None:
    """A ban's length is a matter of record and FPL states it. Press
    speculation about one is not an improvement on the official line."""
    official = _official(NewsCategory.SUSPENDED, None, 0.0)
    assert merge_reported_return(official, _reported(dt.date(2026, 9, 14))) == official


def test_a_report_with_no_date_in_it_changes_nothing() -> None:
    official = _official(NewsCategory.INJURED, None, 0.0)
    assert merge_reported_return(official, _reported(None)) == official


def _seed_article(
    session: Session,
    player: Player,
    *,
    signal: str = RETURN_DATE,
    return_date: dt.date | None = dt.date(2026, 9, 10),
    confidence: float = 0.95,
    published: dt.datetime | None = None,
) -> NewsArticle:
    article = NewsArticle(
        source="BBC Sport",
        guid=f"g{session.query(NewsArticle).count()}",
        url="https://example.invalid/x",
        title="Player out for a fortnight",
        summary="",
        published_at=published or AS_OF - dt.timedelta(hours=2),
        fetched_at=AS_OF,
    )
    session.add(article)
    session.flush()
    session.add(
        NewsMention(
            article_id=article.id,
            player_id=player.id,
            confidence=confidence,
            matched_alias="player",
            match_basis="full_name",
            signal=signal,
            return_date=return_date,
            evidence="Player is out for a fortnight",
        )
    )
    session.flush()
    return article


def _injured(session: Session, player: Player) -> None:
    player.status = "i"
    player.news = "Knee injury - Unknown return date"
    player.chance_of_playing_next_round = 0
    session.flush()


def test_a_low_confidence_match_is_stored_and_never_consumed(db_session: Session) -> None:
    """A maybe-match is still worth a human reading. It is not worth a model
    acting on, and the threshold is what separates the two."""
    make_league(db_session, teams=2)
    player = db_session.query(Player).first()
    assert player is not None
    _injured(db_session, player)
    _seed_article(db_session, player, confidence=0.5)

    assert db_session.query(NewsMention).count() == 1
    assert ExternalNewsSource(min_confidence=0.8).fetch(db_session, AS_OF.date()) == []


def test_a_stale_article_stops_being_consumed(db_session: Session) -> None:
    """A three-week-old "out for a fortnight" is arithmetic about a date that
    has already been superseded."""
    make_league(db_session, teams=2)
    player = db_session.query(Player).first()
    assert player is not None
    _injured(db_session, player)
    _seed_article(db_session, player, published=AS_OF - dt.timedelta(days=40))

    assert ExternalNewsSource(max_age_days=21).fetch(db_session, AS_OF.date()) == []


def test_only_a_dated_return_is_consumed(db_session: Session) -> None:
    make_league(db_session, teams=2)
    player = db_session.query(Player).first()
    assert player is not None
    _injured(db_session, player)
    _seed_article(db_session, player, signal=RETURNING, return_date=None)

    assert ExternalNewsSource().fetch(db_session, AS_OF.date()) == []


def test_the_latest_report_supersedes_the_one_it_revises(db_session: Session) -> None:
    make_league(db_session, teams=2)
    player = db_session.query(Player).first()
    assert player is not None
    _injured(db_session, player)
    _seed_article(
        db_session, player, return_date=dt.date(2026, 11, 1), published=AS_OF - dt.timedelta(days=5)
    )
    _seed_article(
        db_session,
        player,
        return_date=dt.date(2026, 9, 10),
        published=AS_OF - dt.timedelta(hours=1),
    )

    (news,) = ExternalNewsSource().fetch(db_session, AS_OF.date())
    assert news.return_date == dt.date(2026, 9, 10)


def test_a_reported_return_reaches_availability_but_not_the_next_round(
    db_session: Session,
) -> None:
    """The end-to-end path, and the invariant that survives it: a press report
    can lift a later gameweek and can never move the one FPL was describing."""
    teams = make_league(db_session, teams=2)
    for event in (1, 2, 3):
        make_fixture(
            db_session,
            teams[0],
            teams[1],
            fpl_id=event,
            event=event,
            kickoff=AS_OF + dt.timedelta(days=1 + 7 * (event - 1)),
        )
    player = db_session.query(Player).first()
    assert player is not None
    _injured(db_session, player)
    _seed_article(db_session, player, return_date=(AS_OF + dt.timedelta(days=4)).date())

    sources = (FPLPlayerNewsSource(), ExternalNewsSource())
    entry = availability_timeline(db_session, [1, 2, 3], as_of=AS_OF, sources=sources)[player.id]

    assert entry.by_event[1] == 0.0, "FPL said 0% for the next round and still does"
    assert entry.by_event[3] > 0.0, "the reported return lands before the third round"
    assert entry.news.return_date_is_reported


def test_a_reported_return_is_believed_more_slowly_than_an_official_one(
    db_session: Session,
) -> None:
    """A journalist relaying a club's estimate is a real signal and a
    second-hand one, so the ramp around it is stretched rather than trusted."""
    teams = make_league(db_session, teams=2)
    for event in (1, 2, 3):
        make_fixture(
            db_session,
            teams[0],
            teams[1],
            fpl_id=event,
            event=event,
            kickoff=AS_OF + dt.timedelta(days=1 + 7 * (event - 1)),
        )
    players = db_session.query(Player).all()
    official, reported = players[0], players[1]
    back = (AS_OF + dt.timedelta(days=4)).date()

    official.status = "i"
    official.news = f"Knee injury - Expected back {back.day} {back:%b}"
    official.chance_of_playing_next_round = 0
    _injured(db_session, reported)
    _seed_article(db_session, reported, return_date=back)

    timeline = availability_timeline(
        db_session, [1, 2, 3], as_of=AS_OF, sources=(FPLPlayerNewsSource(), ExternalNewsSource())
    )

    assert timeline[official.id].by_event[3] > timeline[reported.id].by_event[3] > 0.0


class _StubFeed:
    def __init__(self, items: list[FeedItem]) -> None:
        self.items = items

    def fetch_all(self) -> list[FeedItem]:
        return self.items

    def close(self) -> None:
        pass


def _item(guid: str, title: str, published: dt.datetime | None = None) -> FeedItem:
    return FeedItem(
        source="BBC Sport",
        guid=guid,
        url=f"https://example.invalid/{guid}",
        title=title,
        summary="",
        published_at=published or AS_OF - dt.timedelta(hours=1),
    )


def test_a_scrape_that_fetches_nothing_is_an_error_not_a_quiet_day(
    db_session: Session,
) -> None:
    """This project has already lost a month to a green job writing an empty
    table. A run that gets nothing from every feed at once is broken, and has
    to say so rather than exiting cleanly."""
    make_league(db_session, teams=2)
    try:
        ingest_news(db_session, client=_StubFeed([]), as_of=AS_OF)
    except EmptyScrapeError:
        return
    raise AssertionError("an empty fetch should have raised")


def test_the_same_story_arriving_again_is_not_new_evidence(db_session: Session) -> None:
    """Feeds carry a story for days. Re-storing it would make a mention look
    fresher than the reporting behind it."""
    make_league(db_session, teams=2)
    item = _item("g1", "Nothing in particular happened")
    first = ingest_news(db_session, client=_StubFeed([item]), as_of=AS_OF)
    second = ingest_news(db_session, client=_StubFeed([item]), as_of=AS_OF)

    assert first.stored == 1
    assert second.stored == 0


def test_an_article_older_than_the_window_is_not_stored(db_session: Session) -> None:
    make_league(db_session, teams=2)
    report = ingest_news(
        db_session,
        client=_StubFeed([_item("old", "Old news", AS_OF - dt.timedelta(days=60))]),
        as_of=AS_OF,
    )
    assert report.stored == 0
    assert report.skipped_old == 1


def test_an_undated_item_is_kept_rather_than_treated_as_old(db_session: Session) -> None:
    """Feeds that omit timestamps omit them on every item, so discarding them
    on age would silently drop a whole publisher."""
    make_league(db_session, teams=2)
    undated = FeedItem("X", "u1", "https://example.invalid/u", "Something", "", None)
    assert ingest_news(db_session, client=_StubFeed([undated]), as_of=AS_OF).stored == 1


def test_pruning_removes_articles_past_the_read_window(db_session: Session) -> None:
    make_league(db_session, teams=2)
    player = db_session.query(Player).first()
    assert player is not None
    _seed_article(db_session, player, published=AS_OF - dt.timedelta(days=60))
    _seed_article(db_session, player, published=AS_OF - dt.timedelta(hours=1))

    assert prune_articles(db_session, as_of=AS_OF) == 1
    assert db_session.query(NewsArticle).count() == 1
    # The mention went with it rather than dangling.
    assert db_session.query(NewsMention).count() == 1


def test_both_endpoints_report_a_reported_return_date_the_same_way(
    db_session: Session, api_client: object
) -> None:
    """The two views built `NewsOut` separately once, and the list quietly
    reported every press-derived return date as FPL's own official line."""
    teams = make_league(db_session, teams=2)
    for event in (1, 2, 3):
        make_fixture(
            db_session,
            teams[0],
            teams[1],
            fpl_id=event,
            event=event,
            kickoff=dt.datetime.now(dt.UTC) + dt.timedelta(days=1 + 7 * (event - 1)),
        )
    player = db_session.query(Player).first()
    assert player is not None
    _injured(db_session, player)
    _seed_article(
        db_session,
        player,
        return_date=(dt.datetime.now(dt.UTC) + dt.timedelta(days=4)).date(),
        published=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
    )
    db_session.commit()

    detail = api_client.get(f"/players/{player.id}").json()["news"]  # type: ignore[attr-defined]
    listed = next(
        row["news"]
        for row in api_client.get("/news").json()  # type: ignore[attr-defined]
        if row["player_id"] == player.id
    )

    assert detail["return_date_is_reported"] is True
    assert listed["return_date_is_reported"] is True
    assert detail["return_date"] == listed["return_date"]


def test_tightening_the_resolver_can_be_applied_to_articles_already_stored(
    db_session: Session,
) -> None:
    """The revocation the articles are stored for. When the matching rules are
    tightened, the fix has to reach rows already written — the feeds will not
    carry those stories again, so there is no other way to correct them."""
    from fplquant.news.ingest_news import reresolve_mentions

    make_league(db_session, teams=2)
    player = db_session.query(Player).first()
    assert player is not None
    article = _seed_article(db_session, player)
    # A match the current rules would never make, as an older run might have.
    db_session.add(
        NewsMention(
            article_id=article.id,
            player_id=player.id + 1,
            confidence=0.7,
            matched_alias="stale",
            match_basis="bare_surname",
            signal="return_date",
            return_date=dt.date(2026, 9, 10),
            evidence="",
        )
    )
    db_session.flush()
    assert db_session.query(NewsMention).count() == 2

    removed, created = reresolve_mentions(db_session, as_of=AS_OF)

    assert removed == 2
    assert created == 0, "the seeded article names nobody the current rules accept"
    assert db_session.query(NewsMention).filter_by(match_basis="bare_surname").count() == 0


def test_a_duration_is_counted_from_when_the_article_was_written(
    db_session: Session,
) -> None:
    """ "Out for six weeks" written on the 28th means back six weeks after the
    28th. Reading it on the 31st does not move the return."""
    make_league(db_session, teams=2)
    player = db_session.query(Player).first()
    assert player is not None
    player.first_name, player.second_name = "Bukayo", "Saka"
    player.web_name = "Saka"
    db_session.flush()

    published = AS_OF - dt.timedelta(days=3)
    ingest_news(
        db_session,
        client=_StubFeed([_item("d1", "Bukayo Saka out for six weeks", published)]),
        as_of=AS_OF,
    )

    mention = db_session.query(NewsMention).one()
    assert mention.return_date == published.date() + dt.timedelta(weeks=6)
