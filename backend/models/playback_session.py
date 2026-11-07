from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Index, event
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from core.timeutil import utcnow


class PlaybackSession(Base):
    __tablename__ = "playback_sessions"
    __table_args__ = (
        Index("idx_playback_sessions_session_key", "session_key"),
        Index("idx_playback_sessions_last_progress_at", "last_progress_at"),
    )

    id               : Mapped[int]            = mapped_column(Integer, primary_key=True)
    user_id          : Mapped[int]            = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    media_id         : Mapped[int]            = mapped_column(ForeignKey("media.id", ondelete="CASCADE"), nullable=False)
    session_key      : Mapped[str]            = mapped_column(String(255), nullable=False, unique=True)
    source           : Mapped[str]            = mapped_column(String(16), nullable=False)  # "plex" | "jellyfin" | "velum"
    device_id        : Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
    last_sequence    : Mapped[int]            = mapped_column(Integer, default=-1, nullable=False)
    duration_seconds : Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    playback_rate    : Mapped[float]          = mapped_column(Float, default=1.0, nullable=False)
    activity_revision : Mapped[int]            = mapped_column(BigInteger, default=0, nullable=False)
    state            : Mapped[str]            = mapped_column(String(16), default="playing", nullable=False)  # "playing" | "paused"
    progress_percent : Mapped[float]          = mapped_column(Float, default=0.0, nullable=False)
    progress_seconds : Mapped[int]            = mapped_column(Integer, default=0, nullable=False)
    started_at       : Mapped[datetime]       = mapped_column(DateTime, default=utcnow, nullable=False)
    last_progress_at : Mapped[datetime]       = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at       : Mapped[datetime]       = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


@event.listens_for(PlaybackSession.progress_seconds, "set")
def _track_progress_advance(target, value, oldvalue, _initiator):
    """Only actual forward position movement keeps a session alive.

    Pause/resume/heartbeat updates can still change updated_at, but they do not
    postpone the six-hour stale-session expiry unless playback advanced.
    """
    try:
        old = int(oldvalue)
    except (TypeError, ValueError):
        old = -1
    try:
        new = int(value)
    except (TypeError, ValueError):
        return value
    if new > old:
        target.last_progress_at = utcnow()
    return value
