"""Velum history resolve and revisioned preferences

Revision ID: velumpref004
Revises: velumhist003
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "velumpref004"
down_revision = "velumhist003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activity_preferences",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("preference_key", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("media_key", sa.String(length=255), nullable=True),
        sa.Column("show_id", sa.Integer(), nullable=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["show_id"], ["shows.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "preference_key", name="uq_activity_preference_user_key"),
    )
    op.create_index(
        "idx_activity_preferences_user_kind",
        "activity_preferences",
        ["user_id", "kind"],
    )
    op.create_index(
        "idx_activity_preferences_user_revision",
        "activity_preferences",
        ["user_id", "revision"],
    )

    op.create_table(
        "activity_preference_mutations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("mutation_id", sa.String(length=96), nullable=False),
        sa.Column("preference_key", sa.String(length=320), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id",
            "mutation_id",
            name="uq_activity_preference_mutation_user",
        ),
    )


def downgrade() -> None:
    op.drop_table("activity_preference_mutations")
    op.drop_index("idx_activity_preferences_user_revision", table_name="activity_preferences")
    op.drop_index("idx_activity_preferences_user_kind", table_name="activity_preferences")
    op.drop_table("activity_preferences")
