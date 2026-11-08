import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.history_sync import freeze_sessions_for_reset
from core.resume import authoritative_resume, normalize_resume_position
from core.session_lease import lease_deadline
from models.base import MediaType
from models.media import Media
from models.playback_session import PlaybackSession


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class AuthoritativeResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.t0 = datetime(2026, 11, 8, 12, 0, 0)
        self.media = Media(
            id=10,
            media_type=MediaType.movie,
            tmdb_id=550,
            media_key="movie:tmdb:550",
            title="Fight Club",
            runtime=120,
        )

    def test_resume_position_is_clamped_and_threshold_is_not_resumable(self):
        position, completed = normalize_resume_position(8000, 7200)
        self.assertEqual(position, 0)
        self.assertTrue(completed)

    async def test_live_session_beats_history(self):
        session = PlaybackSession(
            id=1,
            user_id=7,
            media_id=10,
            session_key="velum:7:s1",
            client_session_id="s1",
            source="velum",
            device_id="d1",
            state="playing",
            progress_seconds=600,
            progress_percent=600 / 7200,
            duration_seconds=7200,
            playback_rate=1.0,
            last_sequence=9,
            started_at=self.t0,
            last_progress_at=self.t0,
            last_event_at=self.t0,
            lease_expires_at=lease_deadline(self.t0),
            updated_at=self.t0,
        )
        db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult(session)))
        resume = await authoritative_resume(
            db,
            7,
            self.media,
            now=self.t0 + timedelta(seconds=30),
        )
        self.assertEqual(resume.source, "session")
        self.assertEqual(resume.source_session_id, "s1")
        self.assertEqual(resume.resume_position_seconds, 630)

    async def test_history_is_used_when_no_live_session(self):
        activity = SimpleNamespace(
            status="watching",
            position_seconds=900,
            duration_seconds=7200,
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[_ScalarResult(None), _ScalarResult(activity)]
            )
        )
        resume = await authoritative_resume(db, 7, self.media, now=self.t0)
        self.assertEqual(resume.source, "history")
        self.assertEqual(resume.resume_position_seconds, 900)
        self.assertIsNone(resume.source_session_id)

    async def test_watched_or_missing_state_resumes_from_zero(self):
        activity = SimpleNamespace(
            status="watched",
            position_seconds=7200,
            duration_seconds=7200,
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[_ScalarResult(None), _ScalarResult(activity)]
            )
        )
        resume = await authoritative_resume(db, 7, self.media, now=self.t0)
        self.assertEqual(resume.source, "zero")
        self.assertEqual(resume.resume_position_seconds, 0)

    async def test_reset_freezes_existing_sessions(self):
        session = SimpleNamespace(
            playback_rate=1.0,
            state="playing",
            lease_expires_at=self.t0 + timedelta(seconds=60),
            updated_at=self.t0,
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=_ScalarResult([session]))
        )
        await freeze_sessions_for_reset(db, 7, 10)
        self.assertEqual(session.playback_rate, 0)
        self.assertEqual(session.state, "reset")
        self.assertIsNone(session.lease_expires_at)


if __name__ == "__main__":
    unittest.main()
