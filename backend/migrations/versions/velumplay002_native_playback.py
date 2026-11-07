"""Velum native playback sessions

Revision ID: velumplay002
Revises: velumact001
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa


revision = "velumplay002"
down_revision = "velumact001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "playback_sessions",
        sa.Column("activity_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("playback_sessions", "activity_revision")
