from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.activity import assign_media_key, canonical_media_identity
from core.activity_preferences import hidden_next_up_media_keys
from core.history_sync import latest_account_revision
from core.timeutil import utcnow
from db import get_db
from dependencies import get_current_user_or_api_key
from models.media import Media
from models.rewatch import ShowRewatch
from models.users import User, UserSettings
from routers.history import get_continue_watching, get_next_up


router = APIRouter()


def _metadata_from_raw(raw: dict) -> dict:
    return {
        "title": raw.get("title"),
        "overview": raw.get("overview"),
        "poster_path": raw.get("poster_path"),
        "backdrop_path": raw.get("backdrop_path"),
        "release_date": raw.get("release_date"),
        "runtime": raw.get("runtime"),
        "tmdb_rating": raw.get("tmdb_rating"),
        "user_rating": raw.get("user_rating"),
        "show_title": raw.get("show_title"),
        "show_poster_path": raw.get("show_poster_path"),
        "show_backdrop_path": raw.get("show_backdrop_path"),
    }


async def _load_media_map(db: AsyncSession, media_ids: list[int]) -> dict[int, Media]:
    if not media_ids:
        return {}
    rows = (
        await db.execute(
            select(Media)
            .options(selectinload(Media.show))
            .where(Media.id.in_(set(media_ids)))
        )
    ).scalars().all()
    for media in rows:
        assign_media_key(media)
    await db.flush()
    return {media.id: media for media in rows}


def _canonical_shelf_media(media: Media, raw: dict) -> dict:
    identity = canonical_media_identity(media, show=media.show)
    return {
        **identity,
        **_metadata_from_raw(raw),
    }


@router.get("/history/continue-watching")
async def get_v1_continue_watching(
    limit: int | None = Query(20, ge=1, le=200),
    include_hidden: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    legacy = await get_continue_watching(
        limit=limit,
        include_hidden=include_hidden,
        db=db,
        current_user=current_user,
    )
    rows = legacy.get("continue_watching") or []
    media_map = await _load_media_map(
        db, [row["media"]["id"] for row in rows if row.get("media", {}).get("id")]
    )

    settings = (
        await db.execute(
            select(UserSettings).where(UserSettings.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    dropped_show_ids = set(settings.dropped_shows or []) if settings else set()

    items = []
    for row in rows:
        raw = row["media"]
        media = media_map.get(raw.get("id"))
        if media is None:
            continue
        if media.show_id in dropped_show_ids:
            continue
        items.append(
            {
                **_canonical_shelf_media(media, raw),
                "status": "watching",
                "progress_percent": round(float(row.get("progress_percent") or 0) * 100, 4),
                "position_seconds": float(row.get("progress_seconds") or 0),
                "updated_at": row.get("watched_at"),
                "dropped": bool(raw.get("dropped")),
            }
        )

    await db.commit()
    return {
        "items": items,
        "generated_at": utcnow().isoformat(),
        "revision": await latest_account_revision(db, current_user.id),
    }


@router.get("/history/next-up")
async def get_v1_next_up(
    limit: int | None = Query(None, ge=1, le=200),
    include_hidden: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    legacy = await get_next_up(
        db=db,
        current_user=current_user,
        limit=limit,
        include_hidden=include_hidden,
    )
    rows = legacy.get("next_up") or []
    media_map = await _load_media_map(
        db, [row["id"] for row in rows if row.get("id")]
    )

    rewatch_show_ids = set(
        (
            await db.execute(
                select(ShowRewatch.show_id).where(
                    ShowRewatch.user_id == current_user.id
                )
            )
        ).scalars().all()
    )
    today = date.today().isoformat()

    hidden_media_keys = (
        await hidden_next_up_media_keys(db, current_user.id)
        if not include_hidden else set()
    )

    items = []
    for raw in rows:
        media = media_map.get(raw.get("id"))
        if media is None:
            continue
        if media.media_key in hidden_media_keys:
            continue
        if media.show_id in rewatch_show_ids:
            reason = "resume_series"
        elif media.release_date and media.release_date >= today:
            reason = "new_episode"
        else:
            reason = "next_episode"

        items.append(
            {
                **_canonical_shelf_media(media, raw),
                "reason": reason,
                "show_last_watched_at": raw.get("show_last_watched_at"),
                "episodes_left": raw.get("episodes_left"),
                "remaining_runtime": raw.get("remaining_runtime"),
                "next_up_hidden": bool(raw.get("next_up_hidden")),
                "dropped": bool(raw.get("dropped")),
            }
        )

    await db.commit()
    return {
        "items": items,
        "generated_at": utcnow().isoformat(),
        "revision": await latest_account_revision(db, current_user.id),
    }
