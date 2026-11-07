from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.activity import assign_media_key
from core.timeutil import utcnow
from models.activity_conflict import ActivityConflictClock
from models.media import Media
from models.playback_session import PlaybackSession


@dataclass
class PlaybackConflictDecision:
    block_all: bool = False
    block_current_state: bool = False
    reason: str | None = None


async def get_conflict_clock(
    db: AsyncSession, user_id: int, media: Media
) -> ActivityConflictClock:
    media_key = assign_media_key(media)
    clock = (
        await db.execute(
            select(ActivityConflictClock).where(
                ActivityConflictClock.user_id == user_id,
                ActivityConflictClock.media_key == media_key,
            )
        )
    ).scalar_one_or_none()
    if clock is None:
        clock = ActivityConflictClock(
            user_id=user_id,
            media_id=media.id,
            media_key=media_key,
            updated_at=utcnow(),
        )
        db.add(clock)
        await db.flush()
    return clock


async def note_explicit_mutation(
    db: AsyncSession,
    user_id: int,
    media: Media,
    occurred_at: datetime,
) -> None:
    clock = await get_conflict_clock(db, user_id, media)
    if (
        clock.last_explicit_mutation_at is None
        or occurred_at > clock.last_explicit_mutation_at
    ):
        clock.last_explicit_mutation_at = occurred_at
    clock.updated_at = utcnow()


def decide_playback_conflict(
    clock: ActivityConflictClock,
    *,
    session_started_at: datetime,
    occurred_at: datetime,
) -> PlaybackConflictDecision:
    if (
        clock.last_explicit_mutation_at is not None
        and occurred_at <= clock.last_explicit_mutation_at
    ):
        return PlaybackConflictDecision(
            block_all=True,
            block_current_state=True,
            reason="older_than_explicit_mutation",
        )

    if (
        clock.last_session_started_at is not None
        and session_started_at < clock.last_session_started_at
    ):
        return PlaybackConflictDecision(
            block_current_state=True,
            reason="older_session",
        )
    return PlaybackConflictDecision()


async def evaluate_playback_conflict(
    db: AsyncSession,
    session: PlaybackSession,
    media: Media,
    occurred_at: datetime,
) -> tuple[ActivityConflictClock, PlaybackConflictDecision]:
    clock = await get_conflict_clock(db, session.user_id, media)
    decision = decide_playback_conflict(
        clock,
        session_started_at=session.started_at,
        occurred_at=occurred_at,
    )

    if not decision.block_all:
        if (
            clock.last_session_started_at is None
            or session.started_at > clock.last_session_started_at
        ):
            clock.last_session_started_at = session.started_at
        if clock.last_event_at is None or occurred_at > clock.last_event_at:
            clock.last_event_at = occurred_at
        clock.updated_at = utcnow()

    return clock, decision
