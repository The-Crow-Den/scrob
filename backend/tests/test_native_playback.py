import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.native_playback import (
    DEFAULT_COMPLETION_THRESHOLD,
    SUPPORTED_EVENT_KINDS,
    _coerce_media_type,
    _naive_utc,
)
from fastapi import HTTPException
from models.base import MediaType
from routers.v1_playback import (
    PlaybackEventBatch,
    PlaybackEventIn,
    PlaybackMultiSessionBatchIn,
    PlaybackSessionIn,
)


class NativePlaybackContractTests(unittest.TestCase):
    def test_completion_threshold_is_95_percent(self):
        self.assertEqual(DEFAULT_COMPLETION_THRESHOLD, 0.95)

    def test_supported_event_kinds_match_contract(self):
        self.assertEqual(
            SUPPORTED_EVENT_KINDS,
            {"started", "progress", "paused", "resumed", "seeked", "stopped", "completed"},
        )

    def test_timezone_is_normalized_for_naive_database_columns(self):
        value = datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc)
        normalized = _naive_utc(value)
        self.assertIsNone(normalized.tzinfo)
        self.assertEqual(normalized, datetime(2026, 11, 7, 22, 0))

    def test_only_movie_and_episode_are_native_playback_types(self):
        self.assertEqual(_coerce_media_type("movie"), MediaType.movie)
        self.assertEqual(_coerce_media_type("episode"), MediaType.episode)
        with self.assertRaises(HTTPException):
            _coerce_media_type("series")

    def test_session_payload_accepts_canonical_episode_identity(self):
        body = PlaybackSessionIn.model_validate(
            {
                "session_id": "0195d",
                "device_id": "velum-ios",
                "media": {
                    "type": "episode",
                    "ids": {"tmdb": 123, "tvdb": 456},
                    "show_ids": {"tmdb": 42, "tvdb": 84},
                    "season": 1,
                    "episode": 4,
                },
                "position_seconds": 0,
                "duration_seconds": 2400,
                "started_at": "2026-11-07T20:15:30Z",
            }
        )
        self.assertEqual(body.media.show_ids["tmdb"], 42)
        self.assertEqual(body.media.ids["tmdb"], 123)

    def test_batch_is_capped_at_100(self):
        event = {
            "event_id": "e",
            "sequence": 1,
            "kind": "progress",
            "position_seconds": 10,
            "occurred_at": "2026-11-07T20:15:30Z",
        }
        batch = PlaybackEventBatch.model_validate({"events": [event]})
        self.assertEqual(len(batch.events), 1)

    def test_event_requires_non_negative_sequence(self):
        with self.assertRaises(Exception):
            PlaybackEventIn.model_validate(
                {
                    "event_id": "e",
                    "sequence": -1,
                    "kind": "progress",
                    "occurred_at": "2026-11-07T20:15:30Z",
                }
            )


    def test_multi_session_batch_accepts_multiple_sessions(self):
        body = PlaybackMultiSessionBatchIn.model_validate(
            {
                "sessions": [
                    {
                        "session_id": "s1",
                        "events": [
                            {
                                "event_id": "e1",
                                "sequence": 2,
                                "kind": "progress",
                                "position_seconds": 20,
                                "occurred_at": "2026-11-07T20:15:30Z",
                            }
                        ],
                    },
                    {
                        "session_id": "s2",
                        "events": [
                            {
                                "event_id": "e2",
                                "sequence": 4,
                                "kind": "paused",
                                "position_seconds": 40,
                                "playback_rate": 0,
                                "occurred_at": "2026-11-07T20:16:30Z",
                            }
                        ],
                    },
                ]
            }
        )
        self.assertEqual([group.session_id for group in body.sessions], ["s1", "s2"])

    def test_multi_session_batch_caps_session_count(self):
        payload = {
            "sessions": [
                {
                    "session_id": f"s{i}",
                    "events": [
                        {
                            "event_id": f"e{i}",
                            "sequence": i,
                            "kind": "progress",
                            "occurred_at": "2026-11-07T20:15:30Z",
                        }
                    ],
                }
                for i in range(26)
            ]
        }
        with self.assertRaises(Exception):
            PlaybackMultiSessionBatchIn.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
