"""add news articles and mentions

Revision ID: c7a4e0b91d38
Revises: b3d21f9c4e17
Create Date: 2026-08-31 19:20:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c7a4e0b91d38"
down_revision: Union[str, Sequence[str], None] = "b3d21f9c4e17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("guid", sa.String(length=512), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.String(length=4096), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "guid", name="uq_news_article_source_guid"),
    )
    op.create_index("ix_news_articles_source", "news_articles", ["source"])
    op.create_index("ix_news_articles_published_at", "news_articles", ["published_at"])

    op.create_table(
        "news_mentions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("matched_alias", sa.String(length=128), nullable=False),
        sa.Column("match_basis", sa.String(length=32), nullable=False),
        sa.Column("signal", sa.String(length=32), nullable=False),
        sa.Column("return_date", sa.Date(), nullable=True),
        sa.Column("evidence", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["news_articles.id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id", "player_id", name="uq_news_mention"),
    )
    op.create_index("ix_news_mentions_article_id", "news_mentions", ["article_id"])
    op.create_index("ix_news_mentions_player_id", "news_mentions", ["player_id"])
    op.create_index("ix_news_mentions_signal", "news_mentions", ["signal"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("news_mentions")
    op.drop_table("news_articles")
