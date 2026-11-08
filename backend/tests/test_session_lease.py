import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.session_lease import (
    PLAYBACK_LEASE_SECONDS,
    effective_position_seconds,
    expire_playback_leases,
    lease_deadline,
    session_projection,
)
from models.playback_session import PlaybackSession
from routers.v1_playback import PlaybackEventIn


class _Scalars:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class PlaybackLeaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.t0 = datetime(2026, 11, 8, 12, 0, 0)

    def make_session(self, **overrides):
        values = dict(
            user_id=1,
            media_id=2,
            session_key="velum:1:session-a",
            client_session_id="session-a",
            source="velum",
            device_id="device-a",
            state="playing",
            progress_seconds=100,
            progress_percent=0.1,
            duration_seconds=1000,
            playback_rate=1.0,
            last_sequence=4,
            started_at=self.t0,
            last_progress_at=self.t0,
            last_event_at=self.t0,
            lease_expires_at=lease_deadline(self.t0),
            updated_at=self.t0,
        )
        values.update(overrides)
        return PlaybackSession(**values)

    def test_lease_is_90_seconds(self):
        self.assertEqual(PLAYBACK_LEASE_SECONDS, 90)
        self.assertEqual(
            lease_deadline(self.t0),
            self.t0 + timedelta(seconds=90),
        )

    def test_effective_position_is_capped_at_lease(self):
        session = self.make_session()
        self.assertEqual(
            effective_position_seconds(
                session,
                now=self.t0 + timedelta(seconds=30),
            ),
            130.0,
        )
        self.assertEqual(
            effective_position_seconds(
                session,
                now=self.t0 + timedelta(minutes=10),
            ),
            190.0,
        )

    def test_paused_position_never_extrapolates(self):
        session = self.make_session(
            state="paused",
            playback_rate=0,
            lease_expires_at=None,
        )
        self.assertEqual(
            effective_position_seconds(
                session,
                now=self.t0 + timedelta(hours=2),
            ),
            100.0,
        )

    async def test_expired_lease_freezes_session_without_completing(self):
        session = self.make_session()
        db = SimpleNamespace(
            execute=AsyncMock(return_value=_Scalars([session]))
        )
        count = await expire_playback_leases(
            db,
            now=self.t0 + timedelta(seconds=120),
        )
        self.assertEqual(count, 1)
        self.assertEqual(session.state, "stale")
        self.assertEqual(session.playback_rate, 0)
        self.assertIsNone(session.lease_expires_at)
        self.assertNotEqual(session.state, "completed")

    def test_session_projection_exposes_recovery_fields(self):
        session = self.make_session()
        projection = session_projection(session, now=self.t0)
        self.assertEqual(projection["session_id"], "session-a")
        self.assertEqual(projection["last_sequence"], 4)
        self.assertEqual(projection["status"], "playing")
        self.assertIn("lease_expires_at", projection)
        self.assertIn("effective_position_seconds", projection)

    def test_terminal_payload_accepts_zero_playback_rate(self):
        event = PlaybackEventIn.model_validate(
            {
                "event_id": "e1",
                "sequence": 5,
                "kind": "stopped",
                "position_seconds": 100,
                "playback_rate": 0,
                "occurred_at": "2026-11-08T12:00:00Z",
            }
        )
        self.assertEqual(event.playback_rate, 0)


if __name__ == "__main__":
    unittest.main()
