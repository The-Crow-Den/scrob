from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ActivityPreference(Base):
    """Current revisioned viewing preference shared by all Velum clients."""

    __tablename__ = "activity_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "preference_key", name="uq_activity_preference_user_key"),
        Index("idx_activity_preferences_user_kind", "user_id", "kind"),
        Index("idx_activity_preferences_user_revision", "user_id", "revision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    preference_key: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    media_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    show_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("shows.id", ondelete="CASCADE"), nullable=True
    )
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class ActivityPreferenceMutation(Base):
    """Idempotency ledger for preference mutations."""

    __tablename__ = "activity_preference_mutations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "mutation_id", name="uq_activity_preference_mutation_user"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    mutation_id: Mapped[str] = mapped_column(String(96), nullable=False)
    preference_key: Mapped[str] = mapped_column(String(320), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
