"""Velum revisioned history sync

Revision ID: velumhist003
Revises: velumplay002
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "velumhist003"
down_revision = "velumplay002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "activity_changes",
        sa.Column("snapshot", postgresql.JSONB(), nullable=True),
    )

    op.create_table(
        "activity_states",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=False),
        sa.Column("media_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("position_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("play_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_watched_at", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_id"], ["media.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "media_id", name="uq_activity_state_user_media"),
        sa.UniqueConstraint("user_id", "media_key", name="uq_activity_state_user_media_key"),
    )
    op.create_index(
        "idx_activity_states_user_updated",
        "activity_states",
        ["user_id", "updated_at", "media_id"],
    )
    op.create_index(
        "idx_activity_states_user_revision",
        "activity_states",
        ["user_id", "revision"],
    )

    # Preserve all existing Scrob viewing state. PlaybackProgress wins as the
    # current status (a user may be rewatching something with old completed
    # plays), while completed WatchEvents remain the historical play count.
    op.execute(
        """
        WITH watched AS (
            SELECT
                user_id,
                media_id,
                COALESCE(SUM(play_count), 0)::int AS play_count,
                MAX(watched_at) AS last_watched_at,
                MAX(created_at) AS last_created_at
            FROM watch_events
            WHERE completed = true
            GROUP BY user_id, media_id
        ),
        keys AS (
            SELECT user_id, media_id FROM watched
            UNION
            SELECT user_id, media_id FROM playback_progress
        )
        INSERT INTO activity_states (
            user_id,
            media_id,
            media_key,
            status,
            progress_percent,
            position_seconds,
            duration_seconds,
            play_count,
            last_watched_at,
            revision,
            updated_at
        )
        SELECT
            k.user_id,
            k.media_id,
            m.media_key,
            CASE WHEN pp.id IS NOT NULL THEN 'watching' ELSE 'watched' END,
            CASE
                WHEN pp.id IS NOT NULL THEN COALESCE(pp.progress_percent, 0)
                ELSE 1
            END,
            CASE
                WHEN pp.id IS NOT NULL THEN COALESCE(pp.progress_seconds, 0)
                ELSE COALESCE(m.runtime, 0) * 60
            END,
            CASE WHEN m.runtime IS NOT NULL THEN m.runtime * 60 ELSE NULL END,
            COALESCE(w.play_count, 0),
            w.last_watched_at,
            0,
            GREATEST(
                COALESCE(pp.updated_at, TIMESTAMP '1970-01-01'),
                COALESCE(w.last_watched_at, w.last_created_at, TIMESTAMP '1970-01-01')
            )
        FROM keys k
        JOIN media m ON m.id = k.media_id
        LEFT JOIN playback_progress pp
            ON pp.user_id = k.user_id AND pp.media_id = k.media_id
        LEFT JOIN watched w
            ON w.user_id = k.user_id AND w.media_id = k.media_id
        """
    )

    op.create_table(
        "history_mutations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("mutation_id", sa.String(length=96), nullable=False),
        sa.Column("media_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "mutation_id", name="uq_history_mutation_user_id"),
    )
    op.create_index(
        "idx_history_mutations_user_media",
        "history_mutations",
        ["user_id", "media_key"],
    )


def downgrade() -> None:
    op.drop_index("idx_history_mutations_user_media", table_name="history_mutations")
    op.drop_table("history_mutations")
    op.drop_index("idx_activity_states_user_revision", table_name="activity_states")
    op.drop_index("idx_activity_states_user_updated", table_name="activity_states")
    op.drop_table("activity_states")
    op.drop_column("activity_changes", "snapshot")
