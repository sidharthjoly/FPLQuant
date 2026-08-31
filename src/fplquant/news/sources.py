"""Where player news comes from.

Two sources, and they are not peers. `FPLPlayerNewsSource` is **authoritative**:
FPL's percentage is the official line and the number every other part of the
model already consumes. `ExternalNewsSource` is **supplementary**: it reads what
`fplquant.news.ingest_news` stored from public football feeds, and it is allowed
to contribute exactly one thing — a return date for a player FPL has ruled out
without giving one.

That asymmetry is the whole safety design, and `authoritative` on the protocol
is how `availability` knows which is which. An external item cannot change a
player's category, cannot rule anybody out, and cannot contradict FPL about the
round FPL was describing. It fills in a blank, or it does nothing.

The reason for the caution is that free text arrives with no player id attached
and has to be resolved against a six-hundred-name pool full of ambiguous
surnames, while availability is a *hard gate* on expected points. See
`fplquant.news.resolve` for the matching rules and `docs/DATA_SOURCES.md` for
the feeds themselves.
"""

import datetime as dt

from sqlalchemy.orm import Session, selectinload

from fplquant.config import settings
from fplquant.form.fixtures import chance_of_playing
from fplquant.models.orm import NewsArticle, NewsMention, Player
from fplquant.news.extract import RETURN_DATE
from fplquant.news.items import NewsCategory, NewsSource, PlayerNews
from fplquant.news.parse import parse_news


class FPLPlayerNewsSource:
    """FPL's own `news` string, as stored on each player by the ingest.

    The anchor deserves a note. `next_round_availability` is taken from
    `chance_of_playing`, not re-derived from the news text, even though the
    text usually restates it. FPL's percentage column is the authoritative
    number and the one every other part of the model already uses; taking it
    from here guarantees this layer and `form.fixtures` cannot disagree about
    the round they are both describing.
    """

    name = "fpl"
    authoritative = True

    def fetch(self, session: Session, as_of: dt.date) -> list[PlayerNews]:
        return [self.read(player, as_of) for player in session.query(Player).all()]

    def read(self, player: Player, as_of: dt.date) -> PlayerNews:
        parsed = parse_news(player.news or "", status=player.status, as_of=as_of)
        return PlayerNews(
            player_id=player.id,
            category=parsed.category,
            headline=player.news or "",
            condition=parsed.condition,
            return_date=parsed.return_date,
            # A ban's end date is a fact; an injury return date is a club's
            # estimate. `availability` leans on this distinction heavily.
            return_is_certain=parsed.category == NewsCategory.SUSPENDED,
            next_round_availability=chance_of_playing(player),
            source=self.name,
        )


class ExternalNewsSource:
    """Return dates recovered from public football feeds.

    Reads rows written by `fplquant.news.ingest_news`, never the network — the
    API and the CLIs must be able to produce a projection without an outbound
    request, and a feed being slow is not allowed to become a slow endpoint.

    Three independent gates stand between an article and a projection, and all
    three have to open:

    1. The signal must be a *dated* return. A story saying somebody is "back in
       training" is stored and shown and consumed by nothing, because there is
       no date in it to build a recovery from.
    2. The player match must clear `news_min_mention_confidence`. Below it the
       mention still exists in the database and in the API — a human reading a
       maybe-match is the right consumer for one — and cannot move a number.
    3. The article must be recent. A three-week-old "out for a month" is
       arithmetic about a date that has already been superseded.

    A fourth gate lives in `availability`, which only accepts a date for a
    player FPL has already ruled out *and* left undated. So even a wrong match
    at full confidence can only affect somebody who is already at zero, and can
    only ever move them upward.
    """

    name = "feeds"
    authoritative = False

    def __init__(self, min_confidence: float | None = None, max_age_days: int | None = None):
        self.min_confidence = (
            min_confidence if min_confidence is not None else settings.news_min_mention_confidence
        )
        self.max_age_days = (
            max_age_days if max_age_days is not None else settings.news_article_max_age_days
        )

    def fetch(self, session: Session, as_of: dt.date) -> list[PlayerNews]:
        cutoff = dt.datetime.combine(
            as_of - dt.timedelta(days=self.max_age_days), dt.time.min, tzinfo=dt.UTC
        )
        mentions = (
            session.query(NewsMention)
            .options(selectinload(NewsMention.article))
            .filter(
                NewsMention.signal == RETURN_DATE,
                NewsMention.return_date.isnot(None),
                NewsMention.confidence >= self.min_confidence,
            )
            .all()
        )

        # Latest article wins where a player has been written about twice: a
        # revised return date supersedes the one it revises, and feeds carry
        # both for days.
        best: dict[int, NewsMention] = {}
        for mention in mentions:
            if _published(mention.article) < cutoff:
                continue
            current = best.get(mention.player_id)
            if current is None or _published(mention.article) > _published(current.article):
                best[mention.player_id] = mention

        return [
            PlayerNews(
                player_id=player_id,
                # Deliberately not a category. This source is not entitled to an
                # opinion on *why* a player is out — only on when a club has
                # said he is back. `availability` keeps FPL's category.
                category=NewsCategory.INJURED,
                headline=mention.article.title,
                condition=None,
                return_date=mention.return_date,
                return_is_certain=False,
                # Filled in by the merge from FPL's own number; a feed never
                # gets to state what this week's availability is.
                next_round_availability=0.0,
                source=f"{self.name}:{mention.article.source}",
            )
            for player_id, mention in best.items()
        ]


def _published(article: NewsArticle) -> dt.datetime:
    """An article's timestamp, aware, with undated items sorted to the bottom."""
    published = article.published_at or dt.datetime.min
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.UTC)
    return published


def default_sources() -> tuple[NewsSource, ...]:
    """FPL first, always, then anything supplementary that is switched on.

    Order is load-bearing: `availability` folds later sources into the first
    one and only the first one is authoritative.
    """
    if settings.news_feeds_feed_the_model:
        return (FPLPlayerNewsSource(), ExternalNewsSource())
    return (FPLPlayerNewsSource(),)
