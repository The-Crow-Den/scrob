"""Velum playback conflict ordering

Revision ID: velumord005
Revises: velumpref004
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa


revision = "velumord005"
down_revision = "velumpref004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_conflict_clocks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("media_key", sa.String(length=255), nullable=False),
        sa.Column("last_explicit_mutation_at", sa.DateTime(), nullable=True),
        sa.Column("last_session_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_id"], ["media.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("user_id", "media_key", name="uq_activity_conflict_user_media"),
    )


def downgrade() -> None:
    op.drop_table("activity_conflict_clocks")
