from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ActivityState(Base):
    __tablename__ = "activity_states"
    __table_args__ = (
        UniqueConstraint("user_id", "media_id", name="uq_activity_state_user_media"),
        UniqueConstraint("user_id", "media_key", name="uq_activity_state_user_media_key"),
        Index("idx_activity_states_user_updated", "user_id", "updated_at", "media_id"),
        Index("idx_activity_states_user_revision", "user_id", "revision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    media_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("media.id", ondelete="CASCADE"), nullable=False
    )
    media_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    position_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    play_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_watched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class HistoryMutation(Base):
    __tablename__ = "history_mutations"
    __table_args__ = (
        UniqueConstraint("user_id", "mutation_id", name="uq_history_mutation_user_id"),
        Index("idx_history_mutations_user_media", "user_id", "media_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    mutation_id: Mapped[str] = mapped_column(String(96), nullable=False)
    media_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
