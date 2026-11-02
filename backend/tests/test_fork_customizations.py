import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.episode_order import (
    _metadata_is_likely_anime,
    get_episode_order,
    get_episode_orders_for_series,
    get_positions_for_series,
)
from models.episode_order import EpisodeOrderMapping, UserShowEpisodeOrder
from routers.history import (
    _has_confirmed_release_date,
    _mapped_tvdb_successor,
    _recent_rewatch_anchors,
)


class _Result:
    def __init__(self, item=None):
        self.item = item

    def scalar_one_or_none(self):
        return self.item

    def scalars(self):
        return self

    def all(self):
        if isinstance(self.item, list):
            return self.item
        return [] if self.item is None else [self.item]


class ForkAnimeEpisodeOrderTests(unittest.IsolatedAsyncioTestCase):
    def test_classifier_requires_japanese_animation(self):
        self.assertTrue(_metadata_is_likely_anime(
            {"original_language": "ja", "genres": ["Animation", "Drama"]}
        ))
        self.assertFalse(_metadata_is_likely_anime(
            {"original_language": "en", "genres": ["Animation"]}
        ))
        self.assertFalse(_metadata_is_likely_anime(
            {"original_language": "ja", "genres": ["Drama"]}
        ))

    async def test_explicit_aired_override_beats_automatic_default(self):
        explicit = UserShowEpisodeOrder(
            user_id=7, series_tmdb_id=100, episode_order="tmdb:aired"
        )
        db = SimpleNamespace(execute=AsyncMock(return_value=_Result(explicit)))
        result = await get_episode_order(db, 7, 100)
        self.assertIs(result, explicit)
        self.assertEqual(db.execute.await_count, 1)

    async def test_cached_anime_gets_virtual_tvdb_official_default(self):
        anime = SimpleNamespace(
            tmdb_id=100,
            tvdb_id=900,
            tmdb_data={"original_language": "ja", "genres": ["Animation"]},
        )
        settings = SimpleNamespace(tmdb_api_key="tmdb", tvdb_api_key="tvdb")
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result(None),       # explicit preference
            _Result(settings),   # effective keys
            _Result(anime),      # show
        ]))
        pref = await get_episode_order(db, 7, 100)
        self.assertEqual(pref.episode_order, "tvdb:official")
        self.assertEqual(pref.tvdb_id, 900)

    async def test_batch_auto_default_is_cached_only_and_keeps_explicit(self):
        explicit = UserShowEpisodeOrder(
            user_id=7, series_tmdb_id=100, episode_order="tmdb:aired"
        )
        anime = SimpleNamespace(
            tmdb_id=200,
            tvdb_id=901,
            tmdb_data={"original_language": "ja", "genres": ["Animation"]},
        )
        settings = SimpleNamespace(tmdb_api_key=None, tvdb_api_key="tvdb")
        global_settings = SimpleNamespace(tmdb_api_key=None, tvdb_api_key=None)
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result([explicit]),
            _Result(settings),
            _Result(global_settings),
            _Result([anime]),
        ]))
        prefs = await get_episode_orders_for_series(db, 7, [100, 200])
        self.assertEqual(prefs[100].episode_order, "tmdb:aired")
        self.assertEqual(prefs[200].episode_order, "tvdb:official")

    async def test_official_positions_can_come_from_cached_mapping(self):
        mapping = EpisodeOrderMapping(
            series_tmdb_id=100,
            tmdb_season_number=1,
            tmdb_episode_number=13,
            tmdb_episode_id=5013,
            tvdb_id=9013,
            tvdb_season_number=2,
            tvdb_episode_number=1,
            match_method="external_id",
        )
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result([]),
            _Result([mapping]),
        ]))
        maps = await get_positions_for_series(db, [(100, "tvdb:official")])
        pos = maps[(100, "tvdb:official")][(1, 13)]
        self.assertEqual((pos.display_season, pos.display_episode), (2, 1))


class ForkRecentNextUpTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 12, 0, 0)
        self.cutoff = self.now - timedelta(days=14)

    def row(self, episode, *, watched_at=None, created_at=None, media_id=None):
        watched_at = watched_at or self.now
        created_at = created_at or watched_at
        return (55, 3, episode, watched_at, created_at, media_id or episode)

    def test_recent_backward_watch_becomes_anchor(self):
        anchors = _recent_rewatch_anchors(
            [self.row(5)], {55: (3, 7)}, cutoff=self.cutoff
        )
        self.assertEqual(anchors[55][:2], (3, 5))

    def test_normal_furthest_watch_does_not_create_anchor(self):
        self.assertEqual(
            _recent_rewatch_anchors(
                [self.row(7)], {55: (3, 7)}, cutoff=self.cutoff
            ),
            {},
        )

    def test_bulk_burst_is_suppressed(self):
        rows = [self.row(ep, media_id=100 + ep) for ep in (5, 4, 3, 2)]
        self.assertEqual(
            _recent_rewatch_anchors(rows, {55: (3, 7)}, cutoff=self.cutoff),
            {},
        )

    def test_tvdb_successor_must_be_immediate(self):
        anchor = EpisodeOrderMapping(
            series_tmdb_id=100, tmdb_season_number=3, tmdb_episode_number=7,
            tmdb_episode_id=307, tvdb_id=1007, tvdb_season_number=3,
            tvdb_episode_number=7, match_method="external_id",
        )
        successor = EpisodeOrderMapping(
            series_tmdb_id=100, tmdb_season_number=3, tmdb_episode_number=8,
            tmdb_episode_id=308, tvdb_id=1008, tvdb_season_number=3,
            tvdb_episode_number=8, match_method="external_id",
        )
        self.assertEqual(
            _mapped_tvdb_successor(
                100, (3, 7), {(100, 3, 7): anchor, (100, 3, 8): successor}
            ),
            (3, 8),
        )


class ForkUpcomingNextUpTests(unittest.TestCase):
    def test_future_dated_successor_stays_visible(self):
        self.assertTrue(_has_confirmed_release_date("2026-11-09"))

    def test_missing_date_placeholder_stays_hidden(self):
        self.assertFalse(_has_confirmed_release_date(None))
        self.assertFalse(_has_confirmed_release_date(""))


if __name__ == "__main__":
    unittest.main()
