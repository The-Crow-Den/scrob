from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserActivityRevision(Base):
    """Monotonic per-account activity revision used by Velum sync."""

    __tablename__ = "user_activity_revisions"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ActivityChange(Base):
    """Revisioned upsert/delete marker for incremental activity sync."""

    __tablename__ = "activity_changes"
    __table_args__ = (
        UniqueConstraint("user_id", "revision", name="uq_activity_change_user_revision"),
        Index("idx_activity_changes_user_revision", "user_id", "revision"),
        Index("idx_activity_changes_user_media_key", "user_id", "media_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)  # upsert | delete
    media_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("media.id", ondelete="SET NULL"),
        nullable=True,
    )
    media_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )


class PlaybackEvent(Base):
    """Durable, idempotent event envelope for native playback sessions."""

    __tablename__ = "playback_events"
    __table_args__ = (
        UniqueConstraint(
            "playback_session_id",
            "event_id",
            name="uq_playback_event_session_event",
        ),
        UniqueConstraint(
            "playback_session_id",
            "sequence",
            name="uq_playback_event_session_sequence",
        ),
        Index(
            "idx_playback_events_user_session_sequence",
            "user_id",
            "playback_session_id",
            "sequence",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    playback_session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("playback_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(96), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    position_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    playback_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )
