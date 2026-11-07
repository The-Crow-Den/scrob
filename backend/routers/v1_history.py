from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.activity_preferences import (
    list_activity_preferences,
    resolve_history_identity,
    resolve_series_for_media,
    set_activity_preference,
    set_series_dropped_state,
)
from core.history_sync import (
    apply_explicit_history_mutation,
    decode_history_cursor,
    delete_history_item,
    encode_history_cursor,
    format_activity_item,
    latest_account_revision,
    resolve_media_key,
)
from db import get_db
from dependencies import get_current_user_or_api_key
from models.activity import ActivityChange
from models.activity_state import ActivityState
from models.media import Media
from models.users import User
from routers.v1_playback import MediaIdentityIn
from routers.history import _push_show_dropped_to_providers, _push_watch_state


router = APIRouter()


class HistoryMutationIn(BaseModel):
    status: Literal["unwatched", "watching", "watched"]
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    position_seconds: float | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0)
    occurred_at: datetime
    mutation_id: str = Field(min_length=1, max_length=96)
    migration_source: str | None = Field(default=None, max_length=64)


class NextUpPreferenceIn(BaseModel):
    hidden: bool
    mutation_id: str = Field(min_length=1, max_length=96)


class SeriesStateIn(BaseModel):
    state: Literal["active", "dropped"]
    mutation_id: str = Field(min_length=1, max_length=96)


@router.get("/history")
async def get_history_snapshot(
    cursor: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    stmt = (
        select(ActivityState, Media)
        .join(Media, Media.id == ActivityState.media_id)
        .where(ActivityState.user_id == current_user.id)
    )
    if cursor:
        cursor_time, cursor_media_id = decode_history_cursor(cursor)
        stmt = stmt.where(
            or_(
                ActivityState.updated_at < cursor_time,
                and_(
                    ActivityState.updated_at == cursor_time,
                    ActivityState.media_id < cursor_media_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(
                ActivityState.updated_at.desc(),
                ActivityState.media_id.desc(),
            ).limit(limit + 1)
        )
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [
        await format_activity_item(db, state, media)
        for state, media in page
    ]
    next_cursor = None
    if has_more and page:
        state = page[-1][0]
        next_cursor = encode_history_cursor(state.updated_at, state.media_id)

    return {
        "items": items,
        "preferences": await list_activity_preferences(db, current_user.id),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "latest_revision": await latest_account_revision(db, current_user.id),
    }


@router.get("/history/changes")
async def get_history_changes(
    after_revision: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    rows = (
        await db.execute(
            select(ActivityChange)
            .where(
                ActivityChange.user_id == current_user.id,
                ActivityChange.revision > after_revision,
            )
            .order_by(ActivityChange.revision)
            .limit(limit + 1)
        )
    ).scalars().all()

    has_more = len(rows) > limit
    page = rows[:limit]
    changes = []
    for change in page:
        if change.operation == "preference":
            changes.append(
                {
                    "operation": "preference",
                    "revision": int(change.revision),
                    "preference": change.snapshot or {},
                }
            )
            continue
        if change.operation == "delete":
            changes.append(
                {
                    "operation": "delete",
                    "revision": int(change.revision),
                    "media_key": change.media_key,
                }
            )
            continue

        snapshot = change.snapshot
        if snapshot is None:
            # Compatibility for an upsert created by PR B before PR C was
            # deployed: return the current authoritative state if it still exists.
            state = (
                await db.execute(
                    select(ActivityState, Media)
                    .join(Media, Media.id == ActivityState.media_id)
                    .where(
                        ActivityState.user_id == current_user.id,
                        ActivityState.media_key == change.media_key,
                    )
                )
            ).first()
            snapshot = (
                await format_activity_item(db, state[0], state[1])
                if state else None
            )
        if snapshot is not None:
            changes.append(
                {
                    "operation": "upsert",
                    "revision": int(change.revision),
                    "item": snapshot,
                }
            )

    latest = await latest_account_revision(db, current_user.id)
    return {
        "changes": changes,
        "latest_revision": latest,
        "has_more": has_more,
    }


@router.post("/history/resolve")
async def resolve_history_item(
    body: MediaIdentityIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    return await resolve_history_identity(
        db, current_user.id, body.model_dump()
    )


@router.get("/history/items/{media_key}")
async def get_history_item(
    media_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    row = (
        await db.execute(
            select(ActivityState, Media)
            .join(Media, Media.id == ActivityState.media_id)
            .where(
                ActivityState.user_id == current_user.id,
                ActivityState.media_key == media_key,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="no viewing activity for media_key")
    return await format_activity_item(db, row[0], row[1])


@router.put("/history/items/{media_key}")
async def put_history_item(
    media_key: str,
    body: HistoryMutationIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    media = await resolve_media_key(db, media_key)
    item, duplicate = await apply_explicit_history_mutation(
        db,
        current_user.id,
        media,
        mutation_id=body.mutation_id,
        status=body.status,
        progress_percent=body.progress_percent,
        position_seconds=body.position_seconds,
        duration_seconds=body.duration_seconds,
        occurred_at=body.occurred_at,
        migration_source=body.migration_source,
    )

    if not duplicate and body.status in {"watched", "unwatched"}:
        await _push_watch_state(
            db,
            current_user.id,
            [media.id],
            watched=body.status == "watched",
            watched_at_by_media={media.id: body.occurred_at.replace(tzinfo=None)}
            if body.status == "watched"
            else None,
        )

    return {"duplicate": duplicate, "item": item}


@router.put("/history/items/{media_key}/shelves/next-up")
async def put_next_up_preference(
    media_key: str,
    body: NextUpPreferenceIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    media = await resolve_media_key(db, media_key)
    if media.media_type.value != "episode":
        raise HTTPException(status_code=422, detail="Next Up preference requires an episode")

    preference, duplicate = await set_activity_preference(
        db,
        current_user.id,
        mutation_id=body.mutation_id,
        preference_key=f"next-up:{media_key}",
        kind="next_up_hidden",
        value={"hidden": body.hidden},
        media_key=media_key,
        show_id=media.show_id,
    )
    await db.commit()
    return {"duplicate": duplicate, "preference": preference}


@router.put("/history/items/{media_key}/series-state")
async def put_series_state(
    media_key: str,
    body: SeriesStateIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    media = await resolve_media_key(db, media_key)
    show = await resolve_series_for_media(db, media)
    preference, duplicate = await set_activity_preference(
        db,
        current_user.id,
        mutation_id=body.mutation_id,
        preference_key=f"series-state:{show.id}",
        kind="series_state",
        value={
            "state": body.state,
            "show_ids": {
                "tmdb": show.tmdb_id,
                "tvdb": show.tvdb_id,
            },
        },
        media_key=media_key,
        show_id=show.id,
    )
    settings = None
    if not duplicate:
        settings = await set_series_dropped_state(
            db, current_user.id, show=show, state=body.state
        )
    await db.commit()

    if not duplicate and settings is not None and show.tmdb_id:
        await _push_show_dropped_to_providers(
            db,
            settings,
            show.tmdb_id,
            remove=body.state == "active",
        )

    return {"duplicate": duplicate, "preference": preference}


@router.delete("/history/items/{media_key}")
async def delete_history_item_route(
    media_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    media = await resolve_media_key(db, media_key)
    revision = await delete_history_item(db, current_user.id, media)
    await _push_watch_state(db, current_user.id, [media.id], watched=False)
    return {
        "deleted": True,
        "media_key": media_key,
        "revision": revision,
    }
