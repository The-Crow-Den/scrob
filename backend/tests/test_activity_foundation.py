import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost/test",
)

from core.request_id import RequestIdMiddleware
from core.activity import (
    STALE_PLAYBACK_SESSION_HOURS,
    assign_media_key,
    candidate_media_key,
    canonical_media_identity,
    cleanup_stale_playback_sessions,
)
from models.base import MediaType
from models.media import Media
from models.playback_session import PlaybackSession


class ActivityIdentityTests(unittest.TestCase):
    def test_provider_key_is_assigned_once(self):
        media = Media(
            id=10,
            media_type=MediaType.movie,
            tmdb_id=550,
            title="Fight Club",
        )
        self.assertEqual(candidate_media_key(media), "movie:tmdb:550")
        self.assertEqual(assign_media_key(media), "movie:tmdb:550")

        # Later enrichment must not silently change the client-visible key.
        media.tmdb_id = 551
        self.assertEqual(assign_media_key(media), "movie:tmdb:550")

    def test_fallback_key_uses_scrob_id(self):
        media = Media(id=42, media_type=MediaType.movie, title="Unknown")
        self.assertEqual(candidate_media_key(media), "movie:scrob:42")

    def test_episode_identity_keeps_episode_and_show_ids_separate(self):
        show = SimpleNamespace(tmdb_id=100, tvdb_id=200, imdb_id="ttshow")
        media = Media(
            id=7,
            media_type=MediaType.episode,
            tmdb_id=300,
            tvdb_id=400,
            imdb_id="ttepisode",
            title="Episode",
            season_number=2,
            episode_number=3,
        )
        payload = canonical_media_identity(media, show=show)
        self.assertEqual(payload["ids"]["tmdb"], 300)
        self.assertEqual(payload["show_ids"]["tmdb"], 100)
        self.assertEqual(payload["season"], 2)
        self.assertEqual(payload["episode"], 3)


class RequestIdMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_gets_request_id_and_scope_state(self):
        sent = []

        async def app(scope, receive, send):
            self.assertTrue(scope["state"]["request_id"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RequestIdMiddleware(app)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [],
            "state": {},
        }
        await middleware(scope, receive, send)

        headers = dict(sent[0]["headers"])
        self.assertIn(b"x-request-id", headers)
        self.assertEqual(headers[b"x-request-id"].decode("ascii"), scope["state"]["request_id"])


class PlaybackSessionProgressClockTests(unittest.TestCase):
    def test_only_forward_progress_refreshes_last_progress_at(self):
        session = PlaybackSession(
            user_id=1,
            media_id=2,
            session_key="velum:test",
            source="velum",
            progress_seconds=10,
        )
        old = datetime(2026, 11, 7, 10, 0, 0)
        session.last_progress_at = old

        session.progress_seconds = 10
        self.assertEqual(session.last_progress_at, old)

        session.progress_seconds = 5
        self.assertEqual(session.last_progress_at, old)

        session.progress_seconds = 11
        self.assertGreater(session.last_progress_at, old)


class _Scalars:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class StalePlaybackSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_hours_is_the_default_expiry(self):
        self.assertEqual(STALE_PLAYBACK_SESSION_HOURS, 6)

    async def test_no_stale_sessions_is_a_noop(self):
        db = SimpleNamespace(execute=AsyncMock(return_value=_Scalars([])))
        count = await cleanup_stale_playback_sessions(
            db, now=datetime(2026, 11, 7, 12, 0, 0)
        )
        self.assertEqual(count, 0)
        self.assertEqual(db.execute.await_count, 1)

    async def test_stale_ids_are_deleted(self):
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[_Scalars([1, 2]), SimpleNamespace()])
        )
        count = await cleanup_stale_playback_sessions(
            db,
            now=datetime(2026, 11, 7, 12, 0, 0),
            stale_after=timedelta(hours=6),
        )
        self.assertEqual(count, 2)
        self.assertEqual(db.execute.await_count, 2)


if __name__ == "__main__":
    unittest.main()
