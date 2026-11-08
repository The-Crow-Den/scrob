from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from core import tmdb
from core.activity import assign_media_key, canonical_media_identity
from core.activity_conflict import evaluate_playback_conflict
from core.history_sync import get_activity_state, sync_historical_completion_only, sync_state_from_playback_session
from core.enrichment import create_media_safely, enrich_media_safely
from core.identity import external_ids_from_tmdb, find_media, find_show, link_media_ids
from core.timeutil import utcnow
from core.session_lease import canonical_session_id, lease_deadline, session_projection
from models.activity import PlaybackEvent
from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.playback_session import PlaybackSession
from models.show import Show
from routers.media import get_user_tmdb_key


def _configured_completion_threshold() -> float:
    try:
        value = float(os.getenv("PLAYBACK_COMPLETION_THRESHOLD", "0.95"))
    except (TypeError, ValueError):
        value = 0.95
    return min(1.0, max(0.5, value))


DEFAULT_COMPLETION_THRESHOLD = _configured_completion_threshold()
MIN_RESUME_PROGRESS = 0.05
SUPPORTED_EVENT_KINDS = {
    "started",
    "progress",
    "paused",
    "resumed",
    "seeked",
    "stopped",
    "completed",
}


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _coerce_media_type(value: str) -> MediaType:
    try:
        media_type = MediaType(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="media.type must be movie or episode") from exc
    if media_type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(status_code=422, detail="native playback supports movie and episode media")
    return media_type


async def _find_media_by_imdb(
    db: AsyncSession, media_type: MediaType, imdb_id: str | None
) -> Media | None:
    if not imdb_id:
        return None
    return (
        await db.execute(
            select(Media)
            .where(Media.media_type == media_type, Media.imdb_id == imdb_id)
            .order_by(Media.id)
        )
    ).scalars().first()


async def _resolve_or_create_show(
    db: AsyncSession,
    user_id: int,
    show_ids: dict[str, Any],
) -> Show | None:
    tmdb_id = show_ids.get("tmdb")
    tvdb_id = show_ids.get("tvdb")
    show = await find_show(db, tmdb_id=tmdb_id, tvdb_id=tvdb_id)
    if show:
        return show
    if not tmdb_id:
        return None

    api_key = await get_user_tmdb_key(db, user_id)
    try:
        data = await tmdb.get_show(int(tmdb_id), api_key=api_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not resolve show metadata") from exc

    ext_tvdb, _imdb = external_ids_from_tmdb(data)
    show = Show(
        tmdb_id=int(tmdb_id),
        tvdb_id=int(tvdb_id) if tvdb_id else ext_tvdb,
        canonical_source="tmdb",
        title=data.get("name") or f"Show {tmdb_id}",
        original_title=data.get("original_name"),
        overview=data.get("overview"),
        poster_path=tmdb.poster_url(data.get("poster_path")),
        backdrop_path=tmdb.poster_url(data.get("backdrop_path"), size="w1280"),
        tmdb_rating=data.get("vote_average"),
        status=data.get("status"),
        tagline=data.get("tagline"),
        first_air_date=data.get("first_air_date"),
        last_air_date=data.get("last_air_date"),
        tmdb_data={
            "genres": [g.get("name") for g in data.get("genres", []) if g.get("name")],
            "external_ids": data.get("external_ids") or {},
            "seasons": data.get("seasons") or [],
        },
    )
    db.add(show)
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        show = await find_show(db, tmdb_id=int(tmdb_id))
        if show is None:
            raise
    return show


async def resolve_native_media(
    db: AsyncSession,
    user_id: int,
    media_ref: dict[str, Any],
) -> Media:
    """Resolve the canonical Media row for a native playback request.

    Existing TMDB/TVDB/IMDb identities are accepted. Missing records are
    materialized from TMDB when enough identity exists. AniList/MAL values are
    accepted in the payload for forward compatibility but are not guessed into
    another provider namespace.
    """
    media_type = _coerce_media_type(str(media_ref.get("type") or ""))
    ids = media_ref.get("ids") or {}

    media_key = media_ref.get("media_key")
    if media_key:
        found = (
            await db.execute(
                select(Media).where(
                    Media.media_type == media_type,
                    Media.media_key == media_key,
                )
            )
        ).scalar_one_or_none()
        if found:
            return found

    found = await find_media(
        db,
        media_type,
        tmdb_id=ids.get("tmdb"),
        tvdb_id=ids.get("tvdb"),
        load_show=media_type == MediaType.episode,
    )
    if not found:
        found = await _find_media_by_imdb(db, media_type, ids.get("imdb"))
    if found:
        assign_media_key(found)
        return found

    api_key = await get_user_tmdb_key(db, user_id)

    if media_type == MediaType.movie:
        tmdb_id = ids.get("tmdb")
        if not tmdb_id:
            raise HTTPException(
                status_code=404,
                detail="Media is not known to Scrob and no TMDB movie id was supplied",
            )
        media, _ = await create_media_safely(
            db,
            int(tmdb_id),
            MediaType.movie,
            title=f"Movie {tmdb_id}",
        )
        media = await enrich_media_safely(db, media, api_key=api_key)
        link_media_ids(media, tvdb_id=ids.get("tvdb"), imdb_id=ids.get("imdb"))
        assign_media_key(media)
        await db.flush()
        return media

    season = media_ref.get("season")
    episode = media_ref.get("episode")
    if season is None or episode is None:
        raise HTTPException(status_code=422, detail="episode media requires season and episode")

    show = await _resolve_or_create_show(db, user_id, media_ref.get("show_ids") or {})
    if show is None:
        raise HTTPException(
            status_code=404,
            detail="Episode show is not known to Scrob and no resolvable show identity was supplied",
        )

    found = (
        await db.execute(
            select(Media).where(
                Media.media_type == MediaType.episode,
                Media.show_id == show.id,
                Media.season_number == int(season),
                Media.episode_number == int(episode),
            ).order_by(Media.id)
        )
    ).scalars().first()
    if found:
        link_media_ids(
            found,
            tmdb_id=ids.get("tmdb"),
            tvdb_id=ids.get("tvdb"),
            imdb_id=ids.get("imdb"),
        )
        assign_media_key(found)
        return found

    media, _ = await create_media_safely(
        db,
        int(ids["tmdb"]) if ids.get("tmdb") else None,
        MediaType.episode,
        tvdb_id=int(ids["tvdb"]) if ids.get("tvdb") else None,
        imdb_id=ids.get("imdb"),
        title=f"Episode {episode}",
        show_id=show.id,
        season_number=int(season),
        episode_number=int(episode),
    )
    if show.tmdb_id:
        media = await enrich_media_safely(
            db,
            media,
            api_key=api_key,
            series_tmdb_id=show.tmdb_id,
        )
    assign_media_key(media)
    await db.flush()
    return media


async def _current_play_count(db: AsyncSession, user_id: int, media_id: int) -> int:
    return int(
        (
            await db.execute(
                select(func.coalesce(func.sum(WatchEvent.play_count), 0)).where(
                    WatchEvent.user_id == user_id,
                    WatchEvent.media_id == media_id,
                    WatchEvent.completed.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )


async def materialized_session_state(
    db: AsyncSession,
    session: PlaybackSession,
    media: Media,
) -> dict[str, Any]:
    # Session endpoints expose both the live session projection and the
    # authoritative account-level activity projection.
    projection = session_projection(session)
    activity = await get_activity_state(db, session.user_id, session.media_id)
    if activity is not None:
        return {
            "media_key": assign_media_key(media),
            "status": session.state,
            "activity_status": activity.status,
            "progress_percent": round(float(activity.progress_percent) * 100, 4),
            "position_seconds": float(activity.position_seconds),
            "duration_seconds": activity.duration_seconds,
            "play_count": int(activity.play_count),
            "updated_at": activity.updated_at,
            "revision": int(activity.revision or 0),
            **projection,
        }

    duration = session.duration_seconds
    progress_percent = session.progress_percent
    if duration and duration > 0:
        progress_percent = max(
            0.0,
            min(1.0, float(session.progress_seconds) / float(duration)),
        )
    return {
        "media_key": assign_media_key(media),
        "status": session.state,
        "activity_status": "watched" if session.state == "completed" else "watching",
        "progress_percent": round(progress_percent * 100, 4),
        "position_seconds": float(session.progress_seconds),
        "duration_seconds": duration,
        "play_count": await _current_play_count(db, session.user_id, session.media_id),
        "updated_at": session.updated_at,
        "revision": int(session.activity_revision or 0),
        **projection,
    }


async def _upsert_resume_progress(
    db: AsyncSession,
    session: PlaybackSession,
    *,
    completed: bool = False,
) -> None:
    if completed:
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == session.user_id,
                PlaybackProgress.media_id == session.media_id,
            )
        )
        return

    pct = float(session.progress_percent or 0.0)
    if pct < MIN_RESUME_PROGRESS or pct >= DEFAULT_COMPLETION_THRESHOLD:
        await db.execute(
            delete(PlaybackProgress).where(
                PlaybackProgress.user_id == session.user_id,
                PlaybackProgress.media_id == session.media_id,
            )
        )
        return

    row = (
        await db.execute(
            select(PlaybackProgress).where(
                PlaybackProgress.user_id == session.user_id,
                PlaybackProgress.media_id == session.media_id,
            )
        )
    ).scalar_one_or_none()
    if row:
        row.progress_percent = pct
        row.progress_seconds = int(session.progress_seconds)
        row.updated_at = utcnow()
    else:
        db.add(
            PlaybackProgress(
                user_id=session.user_id,
                media_id=session.media_id,
                progress_percent=pct,
                progress_seconds=int(session.progress_seconds),
                updated_at=utcnow(),
            )
        )


async def create_or_resume_session_for_media(
    db: AsyncSession,
    user_id: int,
    *,
    session_id: str,
    device_id: str,
    media: Media,
    position_seconds: float,
    duration_seconds: float | None,
    started_at: datetime,
    source: str = "velum",
    session_namespace: str | None = None,
) -> tuple[PlaybackSession, Media, bool]:
    namespace = session_namespace or source
    key = f"{namespace}:{user_id}:{session_id}"
    now = utcnow()

    # Exact client retries remain idempotent even after completion. A replay
    # after completion must use a new logical session id.
    existing = (
        await db.execute(
            select(PlaybackSession).where(PlaybackSession.session_key == key)
        )
    ).scalar_one_or_none()
    if existing:
        if existing.user_id != user_id or existing.media_id != media.id:
            raise HTTPException(
                status_code=409,
                detail={"code": "identity_conflict", "message": "session_id already belongs to different media"},
            )
        if existing.client_session_id is None:
            existing.client_session_id = session_id
        if (
            existing.playback_rate > 0
            and existing.lease_expires_at is not None
            and existing.lease_expires_at <= now
            and existing.state == "playing"
        ):
            existing.playback_rate = 0.0
            existing.state = "stale"
            existing.lease_expires_at = None
            await db.commit()
            await db.refresh(existing)
        return existing, media, False

    # Recover lost client bookkeeping by canonical identity. One unfinished
    # user/device/media session wins; older duplicates are frozen first.
    recoverable = (
        await db.execute(
            select(PlaybackSession)
            .where(
                PlaybackSession.user_id == user_id,
                PlaybackSession.device_id == device_id,
                PlaybackSession.media_id == media.id,
                PlaybackSession.source == source,
                PlaybackSession.state.notin_(["completed", "reset"]),
            )
            .order_by(
                PlaybackSession.started_at.desc(),
                PlaybackSession.updated_at.desc(),
                PlaybackSession.id.desc(),
            )
        )
    ).scalars().all()
    if recoverable:
        canonical = recoverable[0]
        for older in recoverable[1:]:
            older.playback_rate = 0.0
            older.state = "stale"
            older.lease_expires_at = None
            older.updated_at = now
        if canonical.client_session_id is None:
            canonical.client_session_id = canonical_session_id(canonical)
        if (
            canonical.playback_rate > 0
            and canonical.lease_expires_at is not None
            and canonical.lease_expires_at <= now
            and canonical.state == "playing"
        ):
            canonical.playback_rate = 0.0
            canonical.state = "stale"
            canonical.lease_expires_at = None
            canonical.updated_at = now
        await db.commit()
        await db.refresh(canonical)
        return canonical, media, False

    seconds = max(0.0, float(position_seconds or 0))
    duration = float(duration_seconds) if duration_seconds else None
    if duration and duration > 0:
        seconds = min(seconds, duration)
    session = PlaybackSession(
        user_id=user_id,
        media_id=media.id,
        session_key=key,
        source=source,
        device_id=device_id,
        client_session_id=session_id,
        state="playing",
        progress_seconds=int(seconds),
        duration_seconds=duration,
        playback_rate=1.0,
        started_at=_naive_utc(started_at),
        updated_at=now,
        last_progress_at=now,
        last_event_at=now,
        lease_expires_at=lease_deadline(now),
    )
    if session.duration_seconds and session.duration_seconds > 0:
        session.progress_percent = min(1.0, seconds / session.duration_seconds)
    try:
        async with db.begin_nested():
            db.add(session)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(
                select(PlaybackSession).where(PlaybackSession.session_key == key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        if existing.user_id != user_id or existing.media_id != media.id:
            raise HTTPException(
                status_code=409,
                detail={"code": "identity_conflict", "message": "session_id already belongs to different media"},
            )
        return existing, media, False

    _clock, creation_conflict = await evaluate_playback_conflict(
        db, session, media, session.started_at
    )
    if not creation_conflict.block_current_state:
        await _upsert_resume_progress(db, session)
        await sync_state_from_playback_session(db, session, media, completed=False)
    else:
        session.playback_rate = 0.0
        session.state = "stale"
        session.lease_expires_at = None
    await db.commit()
    await db.refresh(session)
    return session, media, True


async def create_or_resume_native_session(
    db: AsyncSession,
    user_id: int,
    *,
    session_id: str,
    device_id: str,
    media_ref: dict[str, Any],
    position_seconds: float,
    duration_seconds: float | None,
    started_at: datetime,
) -> tuple[PlaybackSession, Media, bool]:
    media = await resolve_native_media(db, user_id, media_ref)
    return await create_or_resume_session_for_media(
        db,
        user_id,
        session_id=session_id,
        device_id=device_id,
        media=media,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        started_at=started_at,
        source="velum",
        session_namespace="velum",
    )


async def apply_native_playback_event(
    db: AsyncSession,
    session: PlaybackSession,
    media: Media,
    *,
    event_id: str,
    sequence: int,
    kind: str,
    position_seconds: float | None,
    duration_seconds: float | None,
    playback_rate: float | None,
    occurred_at: datetime,
    event_source: str = "velum",
) -> dict[str, Any]:
    if kind not in SUPPORTED_EVENT_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_event_kind", "message": f"unsupported event kind: {kind}"},
        )

    duplicate = (
        await db.execute(
            select(PlaybackEvent).where(
                PlaybackEvent.playback_session_id == session.id,
                PlaybackEvent.event_id == event_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        return {
            "accepted": True,
            "duplicate": True,
            "stale": False,
            "state": await materialized_session_state(db, session, media),
        }

    if session.state == "reset":
        return {
            "accepted": False,
            "duplicate": False,
            "stale": False,
            "error_code": "session_reset",
            "state": await materialized_session_state(db, session, media),
        }

    if session.state == "completed":
        return {
            "accepted": False,
            "duplicate": False,
            "stale": False,
            "error_code": "session_completed",
            "state": await materialized_session_state(db, session, media),
        }

    if sequence <= session.last_sequence:
        return {
            "accepted": False,
            "duplicate": False,
            "stale": True,
            "error_code": "stale_sequence",
            "state": await materialized_session_state(db, session, media),
        }

    received_at = utcnow()
    occurred_at_naive = _naive_utc(occurred_at)
    # Client timestamps remain stored for audit/order, but wildly future
    # clocks cannot become an ordering/lease oracle.
    if occurred_at_naive > received_at:
        occurred_for_order = received_at
    else:
        occurred_for_order = occurred_at_naive

    event = PlaybackEvent(
        user_id=session.user_id,
        playback_session_id=session.id,
        event_id=event_id,
        sequence=sequence,
        kind=kind,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        playback_rate=playback_rate,
        occurred_at=occurred_at_naive,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        duplicate = (
            await db.execute(
                select(PlaybackEvent).where(
                    PlaybackEvent.playback_session_id == session.id,
                    PlaybackEvent.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        if duplicate:
            return {
                "accepted": True,
                "duplicate": True,
                "stale": False,
                "state": await materialized_session_state(db, session, media),
            }
        return {
            "accepted": False,
            "duplicate": False,
            "stale": True,
            "error_code": "stale_sequence",
            "state": await materialized_session_state(db, session, media),
        }

    _clock, conflict = await evaluate_playback_conflict(
        db, session, media, occurred_for_order
    )

    session.last_sequence = sequence
    session.updated_at = received_at

    if conflict.block_all:
        session.playback_rate = 0.0
        session.state = "stale"
        session.lease_expires_at = None
        await db.commit()
        await db.refresh(session)
        return {
            "accepted": False,
            "duplicate": False,
            "stale": False,
            "superseded": True,
            "error_code": "superseded_by_mutation",
            "reason": conflict.reason,
            "state": await materialized_session_state(db, session, media),
        }

    was_completed = session.state == "completed"

    if conflict.block_current_state:
        session.playback_rate = 0.0
        session.state = "stale"
        session.lease_expires_at = None
        if kind == "completed" and not was_completed:
            session.last_event_at = received_at
            db.add(
                WatchEvent(
                    user_id=session.user_id,
                    media_id=session.media_id,
                    watched_at=occurred_at_naive,
                    progress_seconds=int(position_seconds or session.progress_seconds or 0),
                    progress_percent=1.0,
                    completed=True,
                    play_count=1,
                    source=event_source,
                )
            )
            await db.flush()
            await sync_historical_completion_only(
                db, session, media, occurred_at_naive
            )
            await db.commit()
            await db.refresh(session)
            return {
                "accepted": True,
                "duplicate": False,
                "stale": False,
                "superseded": True,
                "reason": conflict.reason,
                "state": await materialized_session_state(db, session, media),
            }

        await db.commit()
        await db.refresh(session)
        return {
            "accepted": False,
            "duplicate": False,
            "stale": False,
            "superseded": True,
            "error_code": "superseded_session",
            "reason": conflict.reason,
            "state": await materialized_session_state(db, session, media),
        }

    # Only an explicit seek may move a live session backwards. For pause/stop
    # transport jitter we preserve the current position while still applying
    # the terminal state transition.
    requested_position = (
        max(0.0, float(position_seconds))
        if position_seconds is not None
        else None
    )
    if (
        kind == "progress"
        and requested_position is not None
        and requested_position < float(session.progress_seconds)
    ):
        await db.commit()
        await db.refresh(session)
        return {
            "accepted": False,
            "duplicate": False,
            "stale": False,
            "error_code": "invalid_position",
            "state": await materialized_session_state(db, session, media),
        }

    session.last_event_at = received_at

    if duration_seconds is not None and duration_seconds > 0:
        session.duration_seconds = float(duration_seconds)

    if requested_position is not None:
        if kind == "seeked":
            session.progress_seconds = int(requested_position)
        else:
            session.progress_seconds = max(
                int(session.progress_seconds),
                int(requested_position),
            )

    if session.duration_seconds and session.duration_seconds > 0:
        # Duration corrections recompute percentage but never move the stored
        # absolute position backwards. Resume responses clamp invalid values.
        session.progress_percent = max(
            0.0,
            min(1.0, session.progress_seconds / session.duration_seconds),
        )

    if kind == "completed":
        session.state = "completed"
        session.playback_rate = 0.0
        session.lease_expires_at = None
        if session.duration_seconds:
            session.progress_seconds = int(session.duration_seconds)
        session.progress_percent = 1.0
    elif kind in {"paused", "stopped"}:
        session.state = kind
        session.playback_rate = 0.0
        session.lease_expires_at = None
    else:
        if playback_rate is not None:
            session.playback_rate = max(0.0, float(playback_rate))
        elif session.playback_rate <= 0 and kind in {"started", "resumed"}:
            session.playback_rate = 1.0
        session.state = "playing" if session.playback_rate > 0 else "paused"
        session.lease_expires_at = (
            lease_deadline(received_at) if session.playback_rate > 0 else None
        )

    if kind == "completed":
        await _upsert_resume_progress(db, session, completed=True)
        if not was_completed:
            db.add(
                WatchEvent(
                    user_id=session.user_id,
                    media_id=session.media_id,
                    watched_at=occurred_at_naive,
                    progress_seconds=int(session.progress_seconds),
                    progress_percent=1.0,
                    completed=True,
                    play_count=1,
                    source=event_source,
                )
            )
    else:
        await _upsert_resume_progress(db, session)

    await db.flush()
    await sync_state_from_playback_session(
        db,
        session,
        media,
        completed=kind == "completed",
        occurred_at=occurred_at_naive if kind == "completed" else None,
    )
    await db.commit()
    await db.refresh(session)

    return {
        "accepted": True,
        "duplicate": False,
        "stale": False,
        "state": await materialized_session_state(db, session, media),
    }


async def get_native_session(
    db: AsyncSession,
    user_id: int,
    session_id: str,
    *,
    session_namespace: str = "velum",
) -> tuple[PlaybackSession, Media]:
    key = f"{session_namespace}:{user_id}:{session_id}"
    session = (
        await db.execute(
            select(PlaybackSession).where(
                PlaybackSession.session_key == key,
                PlaybackSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_session", "message": "playback session not found"},
        )
    media = await db.get(Media, session.media_id)
    if media is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "identity_conflict", "message": "session media no longer exists"},
        )
    return session, media


async def get_active_native_session(
    db: AsyncSession,
    user_id: int,
    *,
    device_id: str,
    media_key: str,
) -> tuple[PlaybackSession, Media] | None:
    media = (
        await db.execute(select(Media).where(Media.media_key == media_key))
    ).scalar_one_or_none()
    if media is None:
        return None

    session = (
        await db.execute(
            select(PlaybackSession)
            .where(
                PlaybackSession.user_id == user_id,
                PlaybackSession.device_id == device_id,
                PlaybackSession.media_id == media.id,
                PlaybackSession.source == "velum",
                PlaybackSession.state.notin_(["completed", "reset"]),
            )
            .order_by(
                PlaybackSession.started_at.desc(),
                PlaybackSession.updated_at.desc(),
                PlaybackSession.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if session is None:
        return None

    now = utcnow()
    if (
        session.playback_rate > 0
        and session.lease_expires_at is not None
        and session.lease_expires_at <= now
        and session.state == "playing"
    ):
        session.playback_rate = 0.0
        session.state = "stale"
        session.lease_expires_at = None
        session.updated_at = now
        await db.commit()
        await db.refresh(session)

    return session, media
