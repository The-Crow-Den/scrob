"""Velum activity foundation

Revision ID: velumact001
Revises: savedflt
Create Date: 2026-11-07
"""

from alembic import op
import sqlalchemy as sa


revision = "velumact001"
down_revision = "savedflt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media", sa.Column("media_key", sa.String(length=255), nullable=True))

    # Prefer a provider-based key when that candidate is unique. Historical
    # duplicate/provider-collision rows fall back to their stable Scrob row id.
    op.execute(
        """
        WITH candidates AS (
            SELECT
                id,
                media_type::text AS kind,
                CASE
                    WHEN tmdb_id IS NOT NULL
                        THEN media_type::text || ':tmdb:' || tmdb_id::text
                    WHEN tvdb_id IS NOT NULL
                        THEN media_type::text || ':tvdb:' || tvdb_id::text
                    WHEN imdb_id IS NOT NULL AND imdb_id <> ''
                        THEN media_type::text || ':imdb:' || imdb_id
                    ELSE NULL
                END AS candidate
            FROM media
        ),
        ranked AS (
            SELECT
                id,
                kind,
                candidate,
                count(*) OVER (PARTITION BY candidate) AS candidate_count
            FROM candidates
        )
        UPDATE media AS m
        SET media_key = CASE
            WHEN r.candidate IS NOT NULL AND r.candidate_count = 1
                THEN r.candidate
            ELSE r.kind || ':scrob:' || r.id::text
        END
        FROM ranked AS r
        WHERE m.id = r.id
        """
    )
    op.create_index("uq_media_media_key", "media", ["media_key"], unique=True)

    op.add_column("playback_sessions", sa.Column("device_id", sa.String(length=255), nullable=True))
    op.add_column(
        "playback_sessions",
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="-1"),
    )
    op.add_column("playback_sessions", sa.Column("duration_seconds", sa.Float(), nullable=True))
    op.add_column(
        "playback_sessions",
        sa.Column("playback_rate", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column(
        "playback_sessions",
        sa.Column("last_progress_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        "UPDATE playback_sessions SET last_progress_at = COALESCE(updated_at, started_at, now())"
    )
    op.alter_column("playback_sessions", "last_progress_at", nullable=False)
    op.create_index(
        "idx_playback_sessions_last_progress_at",
        "playback_sessions",
        ["last_progress_at"],
    )

    op.create_table(
        "user_activity_revisions",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "activity_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("media_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_id"], ["media.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("user_id", "revision", name="uq_activity_change_user_revision"),
    )
    op.create_index(
        "idx_activity_changes_user_revision",
        "activity_changes",
        ["user_id", "revision"],
    )
    op.create_index(
        "idx_activity_changes_user_media_key",
        "activity_changes",
        ["user_id", "media_key"],
    )

    op.create_table(
        "playback_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("playback_session_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("position_seconds", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("playback_rate", sa.Float(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["playback_session_id"],
            ["playback_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "playback_session_id",
            "event_id",
            name="uq_playback_event_session_event",
        ),
        sa.UniqueConstraint(
            "playback_session_id",
            "sequence",
            name="uq_playback_event_session_sequence",
        ),
    )
    op.create_index(
        "idx_playback_events_user_session_sequence",
        "playback_events",
        ["user_id", "playback_session_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("idx_playback_events_user_session_sequence", table_name="playback_events")
    op.drop_table("playback_events")
    op.drop_index("idx_activity_changes_user_media_key", table_name="activity_changes")
    op.drop_index("idx_activity_changes_user_revision", table_name="activity_changes")
    op.drop_table("activity_changes")
    op.drop_table("user_activity_revisions")

    op.drop_index("idx_playback_sessions_last_progress_at", table_name="playback_sessions")
    op.drop_column("playback_sessions", "last_progress_at")
    op.drop_column("playback_sessions", "playback_rate")
    op.drop_column("playback_sessions", "duration_seconds")
    op.drop_column("playback_sessions", "last_sequence")
    op.drop_column("playback_sessions", "device_id")

    op.drop_index("uq_media_media_key", table_name="media")
    op.drop_column("media", "media_key")
