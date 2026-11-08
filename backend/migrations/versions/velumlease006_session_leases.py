"""Velum playback session leases and recovery

Revision ID: velumlease006
Revises: velumord005
Create Date: 2026-11-08
"""

from alembic import op
import sqlalchemy as sa


revision = "velumlease006"
down_revision = "velumord005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "playback_sessions",
        sa.Column("client_session_id", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "playback_sessions",
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "playback_sessions",
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        """
        UPDATE playback_sessions
        SET client_session_id = split_part(session_key, ':', 3)
        WHERE source IN ('velum', 'kodi')
          AND client_session_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE playback_sessions
        SET last_event_at = COALESCE(updated_at, started_at, now())
        WHERE last_event_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE playback_sessions
        SET lease_expires_at = last_event_at + INTERVAL '90 seconds'
        WHERE playback_rate > 0
          AND state = 'playing'
          AND lease_expires_at IS NULL
        """
    )
    op.create_index(
        "idx_playback_sessions_user_device_media_state",
        "playback_sessions",
        ["user_id", "device_id", "media_id", "state"],
    )
    op.create_index(
        "idx_playback_sessions_lease_expires_at",
        "playback_sessions",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_playback_sessions_lease_expires_at", table_name="playback_sessions")
    op.drop_index(
        "idx_playback_sessions_user_device_media_state",
        table_name="playback_sessions",
    )
    op.drop_column("playback_sessions", "lease_expires_at")
    op.drop_column("playback_sessions", "last_event_at")
    op.drop_column("playback_sessions", "client_session_id")
