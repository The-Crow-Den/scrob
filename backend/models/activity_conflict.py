from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ActivityConflictClock(Base):
    """Ordering barriers that survive ActivityState deletion."""

    __tablename__ = "activity_conflict_clocks"
    __table_args__ = (
        UniqueConstraint("user_id", "media_key", name="uq_activity_conflict_user_media"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    media_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("media.id", ondelete="SET NULL"), nullable=True
    )
    media_key: Mapped[str] = mapped_column(String(255), nullable=False)
    last_explicit_mutation_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_session_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
