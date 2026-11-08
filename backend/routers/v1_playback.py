from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.native_playback import (
    DEFAULT_COMPLETION_THRESHOLD,
    apply_native_playback_event,
    create_or_resume_native_session,
    get_native_session,
    materialized_session_state,
)
from db import get_db
from dependencies import get_current_user_or_api_key
from models.users import User


router = APIRouter()
logger = logging.getLogger("scrob.playback")


class MediaIdentityIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    media_key: str | None = None
    type: str
    ids: dict[str, Any] = Field(default_factory=dict)
    show_ids: dict[str, Any] = Field(default_factory=dict)
    season: int | None = None
    episode: int | None = None


class PlaybackSessionIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=160)
    device_id: str = Field(min_length=1, max_length=255)
    media: MediaIdentityIn
    position_seconds: float = Field(default=0, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0)
    started_at: datetime


class PlaybackEventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=96)
    sequence: int = Field(ge=0)
    kind: str
    position_seconds: float | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0)
    playback_rate: float | None = Field(default=None, gt=0)
    occurred_at: datetime


class PlaybackEventBatch(BaseModel):
    events: list[PlaybackEventIn] = Field(min_length=1, max_length=100)


@router.post("/playback/sessions")
async def create_playback_session(
    body: PlaybackSessionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    session, media, created = await create_or_resume_native_session(
        db,
        current_user.id,
        session_id=body.session_id,
        device_id=body.device_id,
        media_ref=body.media.model_dump(),
        position_seconds=body.position_seconds,
        duration_seconds=body.duration_seconds,
        started_at=body.started_at,
    )
    state = await materialized_session_state(db, session, media)
    logger.info(
        "playback_session request_id=%s account=%s device=%s session=%s media_key=%s result=%s revision=%s",
        getattr(request.state, "request_id", None),
        current_user.id,
        body.device_id,
        body.session_id,
        state.get("media_key"),
        "created" if created else "resumed",
        state.get("revision"),
    )
    return {
        "created": created,
        "session_id": body.session_id,
        "completion_threshold": DEFAULT_COMPLETION_THRESHOLD,
        "state": state,
    }


@router.post("/playback/sessions/{session_id}/events")
async def post_playback_events(
    session_id: str,
    request: Request,
    body: PlaybackEventIn | PlaybackEventBatch | list[PlaybackEventIn] = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_or_api_key),
):
    session, media = await get_native_session(db, current_user.id, session_id)

    if isinstance(body, PlaybackEventIn):
        events = [body]
        single = True
    elif isinstance(body, PlaybackEventBatch):
        events = body.events
        single = False
    else:
        events = body
        single = False

    if len(events) > 100:
        raise HTTPException(status_code=422, detail="at most 100 events may be submitted at once")

    # Offline outboxes do not need to preserve transport ordering. Sequence is
    # authoritative, so process a batch from oldest to newest.
    events = sorted(events, key=lambda event: event.sequence)

    results = []
    for event in events:
        result = await apply_native_playback_event(
            db,
            session,
            media,
            event_id=event.event_id,
            sequence=event.sequence,
            kind=event.kind,
            position_seconds=event.position_seconds,
            duration_seconds=event.duration_seconds,
            playback_rate=event.playback_rate,
            occurred_at=event.occurred_at,
        )
        result_state = result.get("state") or {}
        logger.info(
            "playback_event request_id=%s account=%s device=%s session=%s event_id=%s sequence=%s media_key=%s result=%s revision=%s",
            getattr(request.state, "request_id", None),
            current_user.id,
            session.device_id,
            session_id,
            event.event_id,
            event.sequence,
            result_state.get("media_key"),
            "duplicate" if result.get("duplicate") else "stale" if result.get("stale") else "superseded" if result.get("superseded") else "accepted",
            result_state.get("revision"),
        )
        results.append({"event_id": event.event_id, "sequence": event.sequence, **result})

    if single:
        return results[0]
    return {
        "results": results,
        "state": results[-1]["state"] if results else await materialized_session_state(db, session, media),
        "completion_threshold": DEFAULT_COMPLETION_THRESHOLD,
    }
