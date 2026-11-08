from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.playback_policy import COMPLETION_THRESHOLD
from core.session_lease import canonical_session_id, effective_position_seconds
from core.timeutil import utcnow
from models.activity_state import ActivityState
from models.media import Media
from models.playback_session import PlaybackSession


@dataclass
class AuthoritativeResume:
    media_key: str
    resume_position_seconds: float
    duration_seconds: float | None
    source_session_id: str | None
    source: str
    completed_threshold: bool = False

    def as_dict(self) -> dict:
        return {
            "media_key": self.media_key,
            "resume_position_seconds": self.resume_position_seconds,
            "duration_seconds": self.duration_seconds,
            "source_session_id": self.source_session_id,
            "resume_source": self.source,
            "resume_completed_threshold": self.completed_threshold,
        }


def normalize_resume_position(
    position_seconds: float,
    duration_seconds: float | None,
    *,
    completion_threshold: float = COMPLETION_THRESHOLD,
) -> tuple[float, bool]:
    position = max(0.0, float(position_seconds or 0))
    if duration_seconds is None or duration_seconds <= 0:
        return position, False
    duration = float(duration_seconds)
    position = min(position, duration)
    if duration > 0 and position / duration >= completion_threshold:
        return 0.0, True
    return position, False


async def authoritative_resume(
    db: AsyncSession,
    user_id: int,
    media: Media,
    *,
    now: datetime | None = None,
    completion_threshold: float = COMPLETION_THRESHOLD,
) -> AuthoritativeResume:
    now = now or utcnow()
    media_key = media.media_key or f"{media.media_type.value}:scrob:{media.id}"

    # Only genuinely non-stale live/paused sessions outrank history.
    sessions = (
        await db.execute(
            select(PlaybackSession)
            .where(
                PlaybackSession.user_id == user_id,
                PlaybackSession.media_id == media.id,
                PlaybackSession.state.in_(["playing", "paused"]),
                or_(
                    PlaybackSession.state == "paused",
                    PlaybackSession.lease_expires_at.is_(None),
                    PlaybackSession.lease_expires_at > now,
                ),
            )
            .order_by(
                PlaybackSession.started_at.desc(),
                PlaybackSession.updated_at.desc(),
                PlaybackSession.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    if sessions is not None:
        duration = sessions.duration_seconds or (
            float(media.runtime * 60) if media.runtime else None
        )
        raw_position = (
            effective_position_seconds(sessions, now=now)
            if sessions.state == "playing"
            else float(sessions.progress_seconds or 0)
        )
        position, completed = normalize_resume_position(
            raw_position,
            duration,
            completion_threshold=completion_threshold,
        )
        if not completed:
            return AuthoritativeResume(
                media_key=media_key,
                resume_position_seconds=position,
                duration_seconds=duration,
                source_session_id=canonical_session_id(sessions),
                source="session",
            )

    state = (
        await db.execute(
            select(ActivityState).where(
                ActivityState.user_id == user_id,
                ActivityState.media_id == media.id,
            )
        )
    ).scalar_one_or_none()
    if state is not None and state.status == "watching":
        duration = state.duration_seconds or (
            float(media.runtime * 60) if media.runtime else None
        )
        position, completed = normalize_resume_position(
            state.position_seconds,
            duration,
            completion_threshold=completion_threshold,
        )
        if not completed:
            return AuthoritativeResume(
                media_key=media_key,
                resume_position_seconds=position,
                duration_seconds=duration,
                source_session_id=None,
                source="history",
            )
        return AuthoritativeResume(
            media_key=media_key,
            resume_position_seconds=0.0,
            duration_seconds=duration,
            source_session_id=None,
            source="completed_threshold",
            completed_threshold=True,
        )

    duration = (
        state.duration_seconds
        if state is not None and state.duration_seconds
        else float(media.runtime * 60) if media.runtime else None
    )
    return AuthoritativeResume(
        media_key=media_key,
        resume_position_seconds=0.0,
        duration_seconds=duration,
        source_session_id=None,
        source="zero",
    )
