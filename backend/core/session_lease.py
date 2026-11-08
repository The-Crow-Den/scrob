from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.timeutil import utcnow
from models.playback_session import PlaybackSession


PLAYBACK_LEASE_SECONDS = 90
PLAYBACK_LEASE_SWEEP_SECONDS = 30


def lease_deadline(received_at: datetime) -> datetime:
    return received_at + timedelta(seconds=PLAYBACK_LEASE_SECONDS)


def effective_position_seconds(
    session: PlaybackSession,
    *,
    now: datetime | None = None,
) -> float:
    """Return a bounded wall-clock projection without mutating history state."""
    now = now or utcnow()
    position = float(session.progress_seconds or 0)
    if (
        session.playback_rate > 0
        and session.last_event_at is not None
        and session.state == "playing"
    ):
        lease_end = session.lease_expires_at or lease_deadline(session.last_event_at)
        capped_now = min(now, lease_end)
        elapsed = max(0.0, (capped_now - session.last_event_at).total_seconds())
        position += elapsed * float(session.playback_rate)
    if session.duration_seconds and session.duration_seconds > 0:
        position = min(position, float(session.duration_seconds))
    return max(0.0, position)


def canonical_session_id(session: PlaybackSession) -> str:
    if session.client_session_id:
        return session.client_session_id
    parts = (session.session_key or "").split(":", 2)
    return parts[2] if len(parts) == 3 else session.session_key


def session_projection(
    session: PlaybackSession,
    *,
    now: datetime | None = None,
) -> dict:
    return {
        "session_id": canonical_session_id(session),
        "status": session.state,
        "playback_rate": float(session.playback_rate or 0.0),
        "last_sequence": int(session.last_sequence),
        "last_event_at": session.last_event_at.isoformat() if session.last_event_at else None,
        "lease_expires_at": session.lease_expires_at.isoformat() if session.lease_expires_at else None,
        "effective_position_seconds": effective_position_seconds(session, now=now),
    }


async def expire_playback_leases(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Freeze expired live sessions without deleting them or inferring completion."""
    now = now or utcnow()
    rows = (
        await db.execute(
            select(PlaybackSession).where(
                PlaybackSession.playback_rate > 0,
                PlaybackSession.lease_expires_at.isnot(None),
                PlaybackSession.lease_expires_at <= now,
                PlaybackSession.state == "playing",
            )
        )
    ).scalars().all()

    for session in rows:
        # Store the capped projection as the frozen session position only.
        # History/Continue Watching already reflect the last accepted reported
        # position, so lease expiry never invents a completed/progress event.
        session.progress_seconds = int(effective_position_seconds(session, now=now))
        if session.duration_seconds and session.duration_seconds > 0:
            session.progress_percent = min(
                1.0,
                session.progress_seconds / float(session.duration_seconds),
            )
        session.playback_rate = 0.0
        session.state = "stale"
        session.lease_expires_at = None
        session.updated_at = now

    return len(rows)
