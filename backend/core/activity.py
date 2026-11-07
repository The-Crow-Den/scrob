from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.timeutil import utcnow
from models.activity import ActivityChange, UserActivityRevision
from models.media import Media
from models.playback_session import PlaybackSession


STALE_PLAYBACK_SESSION_HOURS = 6
STALE_PLAYBACK_SESSION_SWEEP_SECONDS = 15 * 60


def _media_type_value(media: Media) -> str:
    value = getattr(media.media_type, "value", media.media_type)
    return str(value)


def candidate_media_key(media: Media) -> str:
    """Build the preferred first-assignment key from canonical provider identity.

    The key is persisted on Media and must never be recomputed after assignment,
    because later metadata enrichment can add provider IDs. A client therefore
    treats media_key as opaque and stable.
    """
    kind = _media_type_value(media)
    if media.tmdb_id is not None:
        return f"{kind}:tmdb:{int(media.tmdb_id)}"
    if media.tvdb_id is not None:
        return f"{kind}:tvdb:{int(media.tvdb_id)}"
    if media.imdb_id:
        return f"{kind}:imdb:{media.imdb_id}"
    if media.id is None:
        raise ValueError("Media must be flushed before assigning a fallback media_key")
    return f"{kind}:scrob:{int(media.id)}"


def assign_media_key(media: Media) -> str:
    """Assign a stable media_key once; existing assignments always win."""
    if media.media_key:
        return media.media_key
    media.media_key = candidate_media_key(media)
    return media.media_key


def canonical_media_identity(media: Media, show: Any | None = None) -> dict:
    """Provider-safe identity payload shared by future /v1 endpoints.

    Episode IDs and show IDs intentionally live in separate dictionaries.
    Unknown providers are omitted rather than guessed.
    """
    ids: dict[str, Any] = {}
    if media.tmdb_id is not None:
        ids["tmdb"] = media.tmdb_id
    if media.tvdb_id is not None:
        ids["tvdb"] = media.tvdb_id
    if media.imdb_id:
        ids["imdb"] = media.imdb_id

    result = {
        "media_key": assign_media_key(media),
        "type": _media_type_value(media),
        "ids": ids,
    }

    if _media_type_value(media) == "episode":
        show_ids: dict[str, Any] = {}
        if show is None:
            show = getattr(media, "show", None)
        if show is not None:
            if getattr(show, "tmdb_id", None) is not None:
                show_ids["tmdb"] = show.tmdb_id
            if getattr(show, "tvdb_id", None) is not None:
                show_ids["tvdb"] = show.tvdb_id
            if getattr(show, "imdb_id", None):
                show_ids["imdb"] = show.imdb_id
        result["show_ids"] = show_ids
        result["season"] = media.season_number
        result["episode"] = media.episode_number

    return result


async def next_activity_revision(db: AsyncSession, user_id: int) -> int:
    """Increment and return the account activity revision under a row lock."""
    row = (
        await db.execute(
            select(UserActivityRevision)
            .where(UserActivityRevision.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserActivityRevision(user_id=user_id, revision=1)
        db.add(row)
        await db.flush()
        return 1
    row.revision += 1
    await db.flush()
    return row.revision


async def record_activity_change(
    db: AsyncSession,
    user_id: int,
    *,
    operation: str,
    media: Media | None = None,
    media_key: str | None = None,
) -> int:
    """Append one revisioned activity change.

    Callers own the surrounding transaction so state mutation and revision are
    committed atomically.
    """
    if operation not in {"upsert", "delete"}:
        raise ValueError("operation must be 'upsert' or 'delete'")
    if media is not None:
        media_key = assign_media_key(media)
    if not media_key:
        raise ValueError("media or media_key is required")

    revision = await next_activity_revision(db, user_id)
    db.add(
        ActivityChange(
            user_id=user_id,
            revision=revision,
            operation=operation,
            media_id=media.id if media is not None else None,
            media_key=media_key,
        )
    )
    await db.flush()
    return revision


async def cleanup_stale_playback_sessions(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=STALE_PLAYBACK_SESSION_HOURS),
) -> int:
    """Delete sessions whose playback position has not advanced recently.

    last_progress_at is updated by the PlaybackSession model when
    progress_seconds increases. Existing rows are backfilled from updated_at by
    the migration. PlaybackEvent rows cascade with their parent session.
    """
    now = now or utcnow()
    cutoff = now - stale_after
    ids = list(
        (
            await db.execute(
                select(PlaybackSession.id).where(
                    PlaybackSession.last_progress_at < cutoff
                )
            )
        ).scalars().all()
    )
    if not ids:
        return 0
    await db.execute(delete(PlaybackSession).where(PlaybackSession.id.in_(ids)))
    return len(ids)
