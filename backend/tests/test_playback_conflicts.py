import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.activity_conflict import decide_playback_conflict


class PlaybackConflictOrderingTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 11, 7, 20, 0, 0)

    def clock(self, **kwargs):
        return SimpleNamespace(
            last_explicit_mutation_at=kwargs.get("last_explicit_mutation_at"),
            last_session_started_at=kwargs.get("last_session_started_at"),
            last_event_at=kwargs.get("last_event_at"),
        )

    def test_event_older_than_explicit_mutation_is_fully_blocked(self):
        clock = self.clock(
            last_explicit_mutation_at=self.base + timedelta(minutes=30)
        )
        decision = decide_playback_conflict(
            clock,
            session_started_at=self.base,
            occurred_at=self.base + timedelta(minutes=10),
        )
        self.assertTrue(decision.block_all)
        self.assertTrue(decision.block_current_state)
        self.assertEqual(decision.reason, "older_than_explicit_mutation")

    def test_older_session_cannot_replace_newer_session_state(self):
        clock = self.clock(
            last_session_started_at=self.base + timedelta(hours=1)
        )
        decision = decide_playback_conflict(
            clock,
            session_started_at=self.base,
            occurred_at=self.base + timedelta(hours=2),
        )
        self.assertFalse(decision.block_all)
        self.assertTrue(decision.block_current_state)
        self.assertEqual(decision.reason, "older_session")

    def test_later_session_can_intentionally_restart_title(self):
        clock = self.clock(
            last_session_started_at=self.base
        )
        decision = decide_playback_conflict(
            clock,
            session_started_at=self.base + timedelta(hours=1),
            occurred_at=self.base + timedelta(hours=1),
        )
        self.assertFalse(decision.block_all)
        self.assertFalse(decision.block_current_state)

    def test_same_session_is_ordered_by_sequence_not_conflict_clock(self):
        clock = self.clock(
            last_session_started_at=self.base
        )
        decision = decide_playback_conflict(
            clock,
            session_started_at=self.base,
            occurred_at=self.base + timedelta(minutes=20),
        )
        self.assertFalse(decision.block_all)
        self.assertFalse(decision.block_current_state)

    def test_delete_barrier_blocks_delayed_pre_delete_event(self):
        # A delete records an explicit-mutation timestamp in the persistent
        # conflict clock even though ActivityState itself is removed.
        deleted_at = self.base + timedelta(hours=2)
        clock = self.clock(last_explicit_mutation_at=deleted_at)
        decision = decide_playback_conflict(
            clock,
            session_started_at=self.base,
            occurred_at=deleted_at - timedelta(seconds=1),
        )
        self.assertTrue(decision.block_all)


if __name__ == "__main__":
    unittest.main()
