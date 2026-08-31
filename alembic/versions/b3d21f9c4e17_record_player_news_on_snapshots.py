"""record player news on snapshots

Revision ID: b3d21f9c4e17
Revises: a1c4e97b2d10
Create Date: 2026-08-31 15:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b3d21f9c4e17"
down_revision: Union[str, Sequence[str], None] = "a1c4e97b2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable rather than defaulted to "", so a snapshot taken before this
    # column existed is distinguishable from one taken on a day the player had
    # no news. The difference matters to a backtest: the first is missing data,
    # the second is a fit player.
    op.add_column("player_snapshots", sa.Column("news", sa.String(length=512), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("player_snapshots", "news")
