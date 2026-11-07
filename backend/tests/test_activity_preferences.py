import os
import unittest
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.activity_preferences import preference_snapshot
from routers.v1_history import NextUpPreferenceIn, SeriesStateIn


class ActivityPreferenceContractTests(unittest.TestCase):
    def test_next_up_preference_payload_is_idempotent_contract(self):
        body = NextUpPreferenceIn.model_validate(
            {"hidden": True, "mutation_id": "m1"}
        )
        self.assertTrue(body.hidden)

    def test_series_state_contract(self):
        self.assertEqual(
            SeriesStateIn.model_validate(
                {"state": "dropped", "mutation_id": "m2"}
            ).state,
            "dropped",
        )
        with self.assertRaises(Exception):
            SeriesStateIn.model_validate({"state": "paused", "mutation_id": "m3"})

    def test_preference_snapshot_is_syncable(self):
        pref = SimpleNamespace(
            kind="next_up_hidden",
            preference_key="next-up:episode:tmdb:123",
            revision=8,
            updated_at=datetime(2026, 11, 7, 23, 0, 0),
            value={"hidden": True},
            media_key="episode:tmdb:123",
            show_id=None,
        )
        data = preference_snapshot(pref)
        self.assertTrue(data["hidden"])
        self.assertEqual(data["revision"], 8)
        self.assertEqual(data["media_key"], "episode:tmdb:123")


if __name__ == "__main__":
    unittest.main()
