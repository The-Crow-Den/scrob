from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.activity import assign_media_key, canonical_media_identity, next_activity_revision
from core.timeutil import utcnow
from models.activity import ActivityChange, UserActivityRevision
from models.activity_state import ActivityState, HistoryMutation
from models.events import WatchEvent
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.show import Show


VALID_HISTORY_STATUSES = {"unwatched", "watching", "watched"}


def encode_history_cursor(updated_at: datetime, media_id: int) -> str:
    payload = json.dumps(
        {"updated_at": updated_at.isoformat(), "media_id": int(media_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_history_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        value = datetime.fromisoformat(payload["updated_at"])
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value, int(payload["media_id"])
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid history cursor") from exc


async def latest_account_revision(db: AsyncSession, user_id: int) -> int:
    value = (
        await db.execute(
            select(UserActivityRevision.revision).where(
                UserActivityRevision.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return int(value or 0)


async def get_activity_state(
    db: AsyncSession, user_id: int, media_id: int
) -> ActivityState | None:
    return (
        await db.execute(
            select(ActivityState).where(
                ActivityState.user_id == user_id,
                ActivityState.media_id == media_id,
            )
        )
    ).scalar_one_or_none()


async def _historical_play_data(
    db: AsyncSession, user_id: int, media_id: int
) -> tuple[int, datetime | None]:
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(WatchEvent.play_count), 0),
                func.max(WatchEvent.watched_at),
            ).where(
                WatchEvent.user_id == user_id,
                WatchEvent.media_id == media_id,
                WatchEvent.completed.is_(True),
            )
        )
    ).one()
    return int(row[0] or 0), row[1]


async def ensure_activity_state(
    db: AsyncSession,
    user_id: int,
    media: Media,
    *,
    status: str | None = None,
) -> ActivityState:
    state = await get_activity_state(db, user_id, media.id)
    if state:
        return state

    progress = (
        await db.execute(
            select(PlaybackProgress).where(
                PlaybackProgress.user_id == user_id,
                PlaybackProgress.media_id == media.id,
            )
        )
    ).scalar_one_or_none()
    play_count, last_watched = await _historical_play_data(db, user_id, media.id)
    if status is None:
        status = "watching" if progress else "watched" if play_count else "unwatched"

    state = ActivityState(
        user_id=user_id,
        media_id=media.id,
        media_key=assign_media_key(media),
        status=status,
        progress_percent=float(progress.progress_percent if progress else (1.0 if status == "watched" else 0.0)),
        position_seconds=float(progress.progress_seconds if progress else ((media.runtime or 0) * 60 if status == "watched" else 0)),
        duration_seconds=float(media.runtime * 60) if media.runtime else None,
        play_count=play_count,
        last_watched_at=last_watched,
        revision=0,
        updated_at=progress.updated_at if progress else last_watched or utcnow(),
    )
    db.add(state)
    await db.flush()
    return state


async def format_activity_item(
    db: AsyncSession, state: ActivityState, media: Media
) -> dict[str, Any]:
    show = await db.get(Show, media.show_id) if media.show_id else None
    identity = canonical_media_identity(media, show=show)
    return {
        **identity,
        "title": media.title,
        "overview": media.overview,
        "poster_path": media.poster_path,
        "backdrop_path": media.backdrop_path,
        "release_date": media.release_date,
        "runtime_seconds": int(media.runtime * 60) if media.runtime else None,
        "status": state.status,
        "progress_percent": round(float(state.progress_percent) * 100, 4),
        "position_seconds": float(state.position_seconds),
        "duration_seconds": state.duration_seconds,
        "play_count": int(state.play_count),
        "last_watched_at": state.last_watched_at.isoformat() if state.last_watched_at else None,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        "revision": int(state.revision),
    }


async def publish_activity_state(
    db: AsyncSession,
    user_id: int,
    state: ActivityState,
    media: Media,
) -> tuple[int, dict[str, Any]]:
    revision = await next_activity_revision(db, user_id)
    state.revision = revision
    state.updated_at = utcnow()
    snapshot = await format_activity_item(db, state, media)
    snapshot["revision"] = revision
    db.add(
        ActivityChange(
            user_id=user_id,
            revision=revision,
            operation="upsert",
            media_id=media.id,
            media_key=assign_media_key(media),
            snapshot=snapshot,
        )
    )
    await db.flush()
    return revision, snapshot


async def publish_tombstone(
    db: AsyncSession, user_id: int, media_key: str
) -> int:
    revision = await next_activity_revision(db, user_id)
    db.add(
        ActivityChange(
            user_id=user_id,
            revision=revision,
            operation="delete",
            media_id=None,
            media_key=media_key,
            snapshot=None,
        )
    )
    await db.flush()
    return revision


async def sync_state_from_playback_session(
    db: AsyncSession,
    session,
    media: Media,
    *,
    completed: bool = False,
    occurred_at: datetime | None = None,
) -> tuple[ActivityState, dict[str, Any]]:
    state = await ensure_activity_state(db, session.user_id, media)
    play_count, last_watched = await _historical_play_data(
        db, session.user_id, media.id
    )
    state.status = "watched" if completed else "watching"
    state.progress_percent = 1.0 if completed else float(session.progress_percent or 0.0)
    state.position_seconds = float(session.progress_seconds or 0)
    state.duration_seconds = session.duration_seconds or (
        float(media.runtime * 60) if media.runtime else None
    )
    state.play_count = play_count
    state.last_watched_at = (
        occurred_at if completed and occurred_at is not None else last_watched
    )
    revision, snapshot = await publish_activity_state(
        db, session.user_id, state, media
    )
    session.activity_revision = revision
    return state, snapshot


async def resolve_media_key(
    db: AsyncSession, media_key: str
) -> Media:
    media = (
        await db.execute(select(Media).where(Media.media_key == media_key))
    ).scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="media_key not found")
    return media


async def apply_explicit_history_mutation(
    db: AsyncSession,
    user_id: int,
    media: Media,
    *,
    mutation_id: str,
    status: str,
    progress_percent: float | None,
    position_seconds: float | None,
    duration_seconds: float | None,
    occurred_at: datetime,
    migration_source: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if status not in VALID_HISTORY_STATUSES:
        raise HTTPException(status_code=422, detail="invalid history status")

    duplicate = (
        await db.execute(
            select(HistoryMutation).where(
                HistoryMutation.user_id == user_id,
                HistoryMutation.mutation_id == mutation_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        state = await ensure_activity_state(db, user_id, media)
        return await format_activity_item(db, state, media), True

    state = await ensure_activity_state(db, user_id, media)
    occurred_at = occurred_at.astimezone(timezone.utc).replace(tzinfo=None) if occurred_at.tzinfo else occurred_at

    # Initial Velum migration must never downgrade a remotely completed item.
    # For non-completed conflicts, keep the state with the later reliable
    # timestamp. Existing state.updated_at is the best server-side timestamp
    # available for legacy Scrob rows.
    preserve_remote = False
    if migration_source == "velum_local_v1":
        if state.status == "watched" and status != "watched":
            preserve_remote = True
        elif state.updated_at and occurred_at < state.updated_at:
            preserve_remote = True

    if preserve_remote:
        db.add(
            HistoryMutation(
                user_id=user_id,
                mutation_id=mutation_id,
                media_key=assign_media_key(media),
                status=status,
                revision=state.revision,
            )
        )
        await db.commit()
        return await format_activity_item(db, state, media), False
    play_count, last_watched = await _historical_play_data(db, user_id, media.id)

    if status == "watched":
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == user_id,
                PlaybackProgress.media_id == media.id,
            )
        )
        duration = duration_seconds or (
            float(media.runtime * 60) if media.runtime else None
        )
        position = position_seconds if position_seconds is not None else duration or 0
        db.add(
            WatchEvent(
                user_id=user_id,
                media_id=media.id,
                watched_at=occurred_at,
                progress_seconds=int(position),
                progress_percent=1.0,
                completed=True,
                play_count=1,
                source="manual",
            )
        )
        await db.flush()
        play_count, last_watched = await _historical_play_data(db, user_id, media.id)
        state.status = "watched"
        state.progress_percent = 1.0
        state.position_seconds = float(position)
        state.duration_seconds = duration
        state.play_count = play_count
        state.last_watched_at = last_watched or occurred_at

    elif status == "watching":
        pct = max(0.0, min(100.0, float(progress_percent or 0.0))) / 100.0
        position = max(0.0, float(position_seconds or 0.0))
        duration = duration_seconds or (
            float(media.runtime * 60) if media.runtime else None
        )
        progress = (
            await db.execute(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user_id,
                    PlaybackProgress.media_id == media.id,
                )
            )
        ).scalar_one_or_none()
        if progress:
            progress.progress_percent = pct
            progress.progress_seconds = int(position)
            progress.updated_at = utcnow()
        else:
            db.add(
                PlaybackProgress(
                    user_id=user_id,
                    media_id=media.id,
                    progress_percent=pct,
                    progress_seconds=int(position),
                    updated_at=utcnow(),
                )
            )
        state.status = "watching"
        state.progress_percent = pct
        state.position_seconds = position
        state.duration_seconds = duration
        state.play_count = play_count
        state.last_watched_at = last_watched

    else:
        # Match Scrob's existing explicit "unwatch item" product rule: reset
        # current progress and remove completed plays for this item.
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == user_id,
                PlaybackProgress.media_id == media.id,
            )
        )
        await db.execute(
            delete(WatchEvent).where(
                WatchEvent.user_id == user_id,
                WatchEvent.media_id == media.id,
            )
        )
        state.status = "unwatched"
        state.progress_percent = 0.0
        state.position_seconds = 0.0
        state.duration_seconds = duration_seconds or (
            float(media.runtime * 60) if media.runtime else None
        )
        state.play_count = 0
        state.last_watched_at = None

    revision, snapshot = await publish_activity_state(db, user_id, state, media)
    db.add(
        HistoryMutation(
            user_id=user_id,
            mutation_id=mutation_id,
            media_key=assign_media_key(media),
            status=status,
            revision=revision,
        )
    )
    await db.commit()
    return snapshot, False


async def delete_history_item(
    db: AsyncSession, user_id: int, media: Media
) -> int:
    media_key = assign_media_key(media)
    await db.execute(
        delete(PlaybackProgress).where(
            PlaybackProgress.user_id == user_id,
            PlaybackProgress.media_id == media.id,
        )
    )
    await db.execute(
        delete(WatchEvent).where(
            WatchEvent.user_id == user_id,
            WatchEvent.media_id == media.id,
        )
    )
    await db.execute(
        delete(ActivityState).where(
            ActivityState.user_id == user_id,
            ActivityState.media_id == media.id,
        )
    )
    revision = await publish_tombstone(db, user_id, media_key)
    await db.commit()
    return revision
