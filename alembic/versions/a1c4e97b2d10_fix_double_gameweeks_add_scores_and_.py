"""Key gameweek stats on the fixture, and carry scorelines, defensive
contribution and FPL's price forecast.

Four changes, all of them closing a gap where data existed and was not kept:

* `player_gameweek_stats` was unique on (player, round). FPL returns one row
  per *fixture*, so a double gameweek silently overwrote one of the two. The
  key becomes (player, round, fixture).
* `historical_player_gameweeks` gains the fixture scoreline. Without it
  `engine.rates.played_fixtures` counts nothing as played and every backtested
  gameweek falls back to the team prior.
* Defensive Contribution, the scoring stat added for 2025-26, was not stored
  anywhere despite being worth ~0.44 points per appearance to a defender.
* FPL's published price-change forecast, new for 2026-27.

Revision ID: a1c4e97b2d10
Revises: 2be79e8ed086
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4e97b2d10"
down_revision: str | None = "2be79e8ed086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Nullable rather than zero-defaulted on purpose: a row written before the
# column existed genuinely has no measurement, and zero would claim it made no
# defensive actions. Nothing downstream may treat the two as the same.
_DEFENSIVE_COLUMNS = (
    "defensive_contribution",
    "clearances_blocks_interceptions",
    "recoveries",
    "tackles",
)


def upgrade() -> None:
    # SQLite cannot drop or add a constraint in place; batch mode rebuilds the
    # table. The existing rows carry a fixture id already, so they survive the
    # rebuild unchanged and satisfy the wider key.
    with op.batch_alter_table("player_gameweek_stats") as batch:
        for column in _DEFENSIVE_COLUMNS:
            batch.add_column(sa.Column(column, sa.Integer(), nullable=True))
        batch.drop_constraint("uq_player_round", type_="unique")
        batch.create_unique_constraint(
            "uq_player_round_fixture", ["player_id", "round", "fixture_fpl_id"]
        )

    with op.batch_alter_table("historical_player_gameweeks") as batch:
        batch.add_column(sa.Column("team_h_score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("team_a_score", sa.Integer(), nullable=True))
        for column in _DEFENSIVE_COLUMNS:
            batch.add_column(sa.Column(column, sa.Integer(), nullable=True))

    with op.batch_alter_table("players") as batch:
        batch.add_column(
            sa.Column(
                "defensive_contribution_per_90",
                sa.Float(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column("price_change_percent", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("price_change_hourly_rate", sa.Float(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("price_change_likelihood", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("players") as batch:
        batch.drop_column("price_change_likelihood")
        batch.drop_column("price_change_hourly_rate")
        batch.drop_column("price_change_percent")
        batch.drop_column("defensive_contribution_per_90")

    with op.batch_alter_table("historical_player_gameweeks") as batch:
        for column in _DEFENSIVE_COLUMNS:
            batch.drop_column(column)
        batch.drop_column("team_a_score")
        batch.drop_column("team_h_score")

    # Going back to a (player, round) key can fail where a double gameweek has
    # since been ingested as two rows. Collapsing them here would destroy the
    # data this migration exists to keep, so the duplicates are left to fail
    # loudly rather than being silently merged.
    with op.batch_alter_table("player_gameweek_stats") as batch:
        batch.drop_constraint("uq_player_round_fixture", type_="unique")
        batch.create_unique_constraint("uq_player_round", ["player_id", "round"])
        for column in _DEFENSIVE_COLUMNS:
            batch.drop_column(column)
