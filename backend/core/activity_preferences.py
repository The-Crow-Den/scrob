from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from core.activity import assign_media_key, next_activity_revision
from core.history_sync import format_activity_item
from core.timeutil import utcnow
from models.activity import ActivityChange
from models.activity_preference import ActivityPreference, ActivityPreferenceMutation
from models.activity_state import ActivityState
from models.base import MediaType
from models.media import Media
from models.show import Show
from models.users import UserSettings


async def resolve_history_identity(
    db: AsyncSession,
    user_id: int,
    identity: dict[str, Any],
) -> dict:
    """Resolve catalogue/provider identity to authoritative current history.

    This never creates viewing activity. Aliases resolve onto the already
    canonical Media row and therefore return that row's persisted media_key.
    """
    media_type_raw = str(identity.get("type") or "")
    try:
        media_type = MediaType(media_type_raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="type must be movie or episode") from exc
    if media_type not in (MediaType.movie, MediaType.episode):
        raise HTTPException(status_code=422, detail="type must be movie or episode")

    ids = identity.get("ids") or {}
    media_key = identity.get("media_key")
    media: Media | None = None

    if media_key:
        media = (
            await db.execute(
                select(Media).where(
                    Media.media_type == media_type,
                    Media.media_key == media_key,
                )
            )
        ).scalar_one_or_none()

    if media is None:
        clauses = []
        if ids.get("tmdb") is not None:
            clauses.append(Media.tmdb_id == int(ids["tmdb"]))
        if ids.get("tvdb") is not None:
            clauses.append(Media.tvdb_id == int(ids["tvdb"]))
        if ids.get("imdb"):
            clauses.append(Media.imdb_id == str(ids["imdb"]))
        for clause in clauses:
            candidate = (
                await db.execute(
                    select(Media)
                    .where(Media.media_type == media_type, clause)
                    .order_by(Media.id)
                )
            ).scalars().first()
            if candidate is not None:
                media = candidate
                break

    if media is None and media_type == MediaType.episode:
        season = identity.get("season")
        episode = identity.get("episode")
        show_ids = identity.get("show_ids") or {}
        if season is not None and episode is not None and show_ids:
            show: Show | None = None
            if show_ids.get("tmdb") is not None:
                show = (
                    await db.execute(
                        select(Show).where(Show.tmdb_id == int(show_ids["tmdb"]))
                    )
                ).scalars().first()
            if show is None and show_ids.get("tvdb") is not None:
                show = (
                    await db.execute(
                        select(Show).where(Show.tvdb_id == int(show_ids["tvdb"]))
                    )
                ).scalars().first()
            if show is not None:
                media = (
                    await db.execute(
                        select(Media).where(
                            Media.media_type == MediaType.episode,
                            Media.show_id == show.id,
                            Media.season_number == int(season),
                            Media.episode_number == int(episode),
                        )
                    )
                ).scalars().first()

    if media is None:
        raise HTTPException(status_code=404, detail="no viewing activity for identity")

    state = (
        await db.execute(
            select(ActivityState).where(
                ActivityState.user_id == user_id,
                ActivityState.media_id == media.id,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        raise HTTPException(status_code=404, detail="no viewing activity for identity")

    assign_media_key(media)
    await db.flush()
    return await format_activity_item(db, state, media)


def preference_snapshot(pref: ActivityPreference) -> dict:
    base = {
        "kind": pref.kind,
        "preference_key": pref.preference_key,
        "revision": int(pref.revision),
        "updated_at": pref.updated_at.isoformat() if pref.updated_at else None,
        **(pref.value or {}),
    }
    if pref.media_key:
        base["media_key"] = pref.media_key
    if pref.show_id is not None:
        base["show_id"] = pref.show_id
    return base


async def list_activity_preferences(
    db: AsyncSession, user_id: int
) -> list[dict]:
    rows = (
        await db.execute(
            select(ActivityPreference)
            .where(ActivityPreference.user_id == user_id)
            .order_by(ActivityPreference.kind, ActivityPreference.preference_key)
        )
    ).scalars().all()
    return [preference_snapshot(row) for row in rows]


async def set_activity_preference(
    db: AsyncSession,
    user_id: int,
    *,
    mutation_id: str,
    preference_key: str,
    kind: str,
    value: dict,
    media_key: str | None = None,
    show_id: int | None = None,
) -> tuple[dict, bool]:
    duplicate = (
        await db.execute(
            select(ActivityPreferenceMutation).where(
                ActivityPreferenceMutation.user_id == user_id,
                ActivityPreferenceMutation.mutation_id == mutation_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        current = (
            await db.execute(
                select(ActivityPreference).where(
                    ActivityPreference.user_id == user_id,
                    ActivityPreference.preference_key == duplicate.preference_key,
                )
            )
        ).scalar_one_or_none()
        if current is None:
            raise HTTPException(status_code=409, detail="preference mutation no longer resolves")
        return preference_snapshot(current), True

    pref = (
        await db.execute(
            select(ActivityPreference).where(
                ActivityPreference.user_id == user_id,
                ActivityPreference.preference_key == preference_key,
            )
        )
    ).scalar_one_or_none()
    if pref is None:
        pref = ActivityPreference(
            user_id=user_id,
            preference_key=preference_key,
            kind=kind,
            media_key=media_key,
            show_id=show_id,
            value=dict(value),
            revision=0,
            updated_at=utcnow(),
        )
        db.add(pref)
        await db.flush()
    else:
        pref.kind = kind
        pref.media_key = media_key
        pref.show_id = show_id
        pref.value = dict(value)
        flag_modified(pref, "value")
        pref.updated_at = utcnow()

    revision = await next_activity_revision(db, user_id)
    pref.revision = revision
    snapshot = preference_snapshot(pref)
    snapshot["revision"] = revision

    db.add(
        ActivityChange(
            user_id=user_id,
            revision=revision,
            operation="preference",
            media_id=None,
            media_key=media_key or preference_key,
            snapshot=snapshot,
        )
    )
    db.add(
        ActivityPreferenceMutation(
            user_id=user_id,
            mutation_id=mutation_id,
            preference_key=preference_key,
            revision=revision,
        )
    )
    await db.flush()
    return snapshot, False


async def hidden_next_up_media_keys(
    db: AsyncSession, user_id: int
) -> set[str]:
    rows = (
        await db.execute(
            select(ActivityPreference).where(
                ActivityPreference.user_id == user_id,
                ActivityPreference.kind == "next_up_hidden",
            )
        )
    ).scalars().all()
    return {
        row.media_key
        for row in rows
        if row.media_key and bool((row.value or {}).get("hidden"))
    }


async def resolve_series_for_media(db: AsyncSession, media: Media) -> Show:
    if media.media_type == MediaType.episode and media.show_id:
        show = await db.get(Show, media.show_id)
        if show is not None:
            return show
    if media.media_type == MediaType.series:
        show = None
        if media.tmdb_id is not None:
            show = (
                await db.execute(select(Show).where(Show.tmdb_id == media.tmdb_id))
            ).scalars().first()
        if show is None and media.tvdb_id is not None:
            show = (
                await db.execute(select(Show).where(Show.tvdb_id == media.tvdb_id))
            ).scalars().first()
        if show is not None:
            return show
    raise HTTPException(status_code=422, detail="media_key does not resolve to a series")


async def set_series_dropped_state(
    db: AsyncSession,
    user_id: int,
    *,
    show: Show,
    state: str,
) -> UserSettings:
    settings = (
        await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
    ).scalar_one_or_none()
    if settings is None:
        raise HTTPException(status_code=404, detail="Settings not found")

    dropped = list(settings.dropped_shows or [])
    if state == "dropped":
        if show.id not in dropped:
            dropped.append(show.id)
    elif state == "active":
        if show.id in dropped:
            dropped.remove(show.id)
    else:
        raise HTTPException(status_code=422, detail="state must be active or dropped")
    settings.dropped_shows = dropped
    flag_modified(settings, "dropped_shows")
    return settings
