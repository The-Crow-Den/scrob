from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from core import tmdb
from core.activity import assign_media_key, canonical_media_identity
from core.history_sync import sync_state_from_playback_session
from core.enrichment import create_media_safely, enrich_media_safely
from core.identity import external_ids_from_tmdb, find_media, find_show, link_media_ids
from core.timeutil import utcnow
from models.activity import PlaybackEvent
from models.base import MediaType
from models.events import WatchEvent
from models.media import Media
from models.playback_progress import PlaybackProgress
from models.playback_session import PlaybackSession
from models.show import Show
from routers.media import get_user_tmdb_key


DEFAULT_COMPLETION_THRESHOLD = 0.95
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
    duration = session.duration_seconds
    progress_percent = session.progress_percent
    if duration and duration > 0:
        progress_percent = max(
            0.0,
            min(1.0, float(session.progress_seconds) / float(duration)),
        )
    status = "watched" if session.state == "completed" else "watching"
    return {
        "media_key": assign_media_key(media),
        "status": status,
        "progress_percent": round(progress_percent * 100, 4),
        "position_seconds": float(session.progress_seconds),
        "duration_seconds": duration,
        "play_count": await _current_play_count(db, session.user_id, session.media_id),
        "updated_at": session.updated_at,
        "revision": int(session.activity_revision or 0),
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
    key = f"velum:{user_id}:{session_id}"
    existing = (
        await db.execute(
            select(PlaybackSession).where(PlaybackSession.session_key == key)
        )
    ).scalar_one_or_none()
    if existing:
        if existing.user_id != user_id or existing.media_id != media.id:
            raise HTTPException(status_code=409, detail="session_id already belongs to different media")
        return existing, media, False

    seconds = max(0, int(position_seconds or 0))
    session = PlaybackSession(
        user_id=user_id,
        media_id=media.id,
        session_key=key,
        source="velum",
        device_id=device_id,
        state="playing",
        progress_seconds=seconds,
        duration_seconds=float(duration_seconds) if duration_seconds else None,
        started_at=_naive_utc(started_at),
        updated_at=utcnow(),
        last_progress_at=utcnow(),
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
            raise HTTPException(status_code=409, detail="session_id already belongs to different media")
        return existing, media, False

    await _upsert_resume_progress(db, session)
    await sync_state_from_playback_session(db, session, media, completed=False)
    await db.commit()
    await db.refresh(session)
    return session, media, True


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
) -> dict[str, Any]:
    if kind not in SUPPORTED_EVENT_KINDS:
        raise HTTPException(status_code=422, detail=f"unsupported event kind: {kind}")

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

    if sequence <= session.last_sequence:
        return {
            "accepted": False,
            "duplicate": False,
            "stale": True,
            "state": await materialized_session_state(db, session, media),
        }

    event = PlaybackEvent(
        user_id=session.user_id,
        playback_session_id=session.id,
        event_id=event_id,
        sequence=sequence,
        kind=kind,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        playback_rate=playback_rate,
        occurred_at=_naive_utc(occurred_at),
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
        # A different event won this sequence number concurrently. Sequence is
        # authoritative, so this request is stale rather than retryable.
        return {
            "accepted": False,
            "duplicate": False,
            "stale": True,
            "state": await materialized_session_state(db, session, media),
        }

    was_completed = session.state == "completed"
    session.last_sequence = sequence
    if duration_seconds is not None and duration_seconds > 0:
        session.duration_seconds = float(duration_seconds)
    if playback_rate is not None and playback_rate > 0:
        session.playback_rate = float(playback_rate)
    if position_seconds is not None:
        session.progress_seconds = max(0, int(position_seconds))

    if session.duration_seconds and session.duration_seconds > 0:
        session.progress_percent = max(
            0.0,
            min(1.0, session.progress_seconds / session.duration_seconds),
        )

    if kind in {"started", "resumed", "progress", "seeked"}:
        session.state = "playing"
    elif kind == "paused":
        session.state = "paused"
    elif kind == "stopped":
        session.state = "stopped"
    elif kind == "completed":
        session.state = "completed"
        if session.duration_seconds:
            session.progress_seconds = int(session.duration_seconds)
        session.progress_percent = 1.0

    session.updated_at = utcnow()

    if kind == "completed":
        await _upsert_resume_progress(db, session, completed=True)
        if not was_completed:
            db.add(
                WatchEvent(
                    user_id=session.user_id,
                    media_id=session.media_id,
                    watched_at=_naive_utc(occurred_at),
                    progress_seconds=int(session.progress_seconds),
                    progress_percent=1.0,
                    completed=True,
                    play_count=1,
                    source="velum",
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
        occurred_at=_naive_utc(occurred_at) if kind == "completed" else None,
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
    db: AsyncSession, user_id: int, session_id: str
) -> tuple[PlaybackSession, Media]:
    key = f"velum:{user_id}:{session_id}"
    session = (
        await db.execute(
            select(PlaybackSession).where(
                PlaybackSession.session_key == key,
                PlaybackSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="playback session not found")
    media = await db.get(Media, session.media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="session media no longer exists")
    return session, media
