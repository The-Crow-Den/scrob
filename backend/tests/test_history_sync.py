import os
import unittest
from datetime import datetime

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.history_sync import (
    VALID_HISTORY_STATUSES,
    decode_history_cursor,
    encode_history_cursor,
)
from routers.v1_history import HistoryMutationIn


class HistorySyncContractTests(unittest.TestCase):
    def test_statuses_match_contract(self):
        self.assertEqual(VALID_HISTORY_STATUSES, {"unwatched", "watching", "watched"})

    def test_cursor_round_trip(self):
        stamp = datetime(2026, 11, 7, 22, 30, 15, 123456)
        cursor = encode_history_cursor(stamp, 42)
        decoded_stamp, media_id = decode_history_cursor(cursor)
        self.assertEqual(decoded_stamp, stamp)
        self.assertEqual(media_id, 42)

    def test_invalid_cursor_is_rejected(self):
        with self.assertRaises(Exception):
            decode_history_cursor("not-a-real-cursor")

    def test_mutation_contract_accepts_migration_source(self):
        body = HistoryMutationIn.model_validate(
            {
                "status": "watching",
                "progress_percent": 32.5,
                "position_seconds": 900,
                "duration_seconds": 2700,
                "occurred_at": "2026-11-07T20:45:44Z",
                "mutation_id": "0195f",
                "migration_source": "velum_local_v1",
            }
        )
        self.assertEqual(body.status, "watching")
        self.assertEqual(body.migration_source, "velum_local_v1")

    def test_progress_percent_is_client_facing_0_to_100(self):
        with self.assertRaises(Exception):
            HistoryMutationIn.model_validate(
                {
                    "status": "watching",
                    "progress_percent": 101,
                    "occurred_at": "2026-11-07T20:45:44Z",
                    "mutation_id": "x",
                }
            )


if __name__ == "__main__":
    unittest.main()
