"""Reading stored press mentions back out, for display.

Separate from `sources` because the two want different things. The source is
answering "may this move a projection", so it filters hard — dated returns
only, above the confidence bar, inside the freshness window. This is answering
"what has been written about this player", so it filters barely at all: a story
that says nothing about fitness is still the thing a person wants to see on a
profile page, and a maybe-match is worth showing *with* its caveat rather than
hiding.
"""

import datetime as dt

from sqlalchemy.orm import Session, selectinload

from fplquant.config import settings
from fplquant.models.orm import NewsArticle, NewsMention

DEFAULT_LIMIT = 8


def recent_mentions(
    session: Session,
    player_id: int,
    limit: int = DEFAULT_LIMIT,
    as_of: dt.datetime | None = None,
) -> list[NewsMention]:
    """The most recently published articles naming this player, newest first.

    Undated articles sort last rather than first: a feed that omits timestamps
    should not crowd out one that provides them.
    """
    as_of = as_of or dt.datetime.now(dt.UTC)
    cutoff = as_of - dt.timedelta(days=settings.news_article_max_age_days)
    mentions = (
        session.query(NewsMention)
        .join(NewsMention.article)
        .options(selectinload(NewsMention.article))
        .filter(NewsMention.player_id == player_id)
        .filter((NewsArticle.published_at.is_(None)) | (NewsArticle.published_at >= cutoff))
        .all()
    )
    mentions.sort(
        key=lambda m: (
            m.article.published_at is not None,
            m.article.published_at or dt.datetime.min,
        ),
        reverse=True,
    )
    return mentions[:limit]
