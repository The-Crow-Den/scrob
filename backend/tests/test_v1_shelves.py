import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from routers.v1_shelves import _metadata_from_raw


class V1ShelvesContractTests(unittest.TestCase):
    def test_metadata_adapter_does_not_mix_provider_identity(self):
        raw = {
            "tmdb_id": 123,
            "show_tmdb_id": 42,
            "title": "Episode",
            "show_title": "Series",
            "poster_path": "/episode.jpg",
        }
        metadata = _metadata_from_raw(raw)
        self.assertNotIn("tmdb_id", metadata)
        self.assertNotIn("show_tmdb_id", metadata)
        self.assertEqual(metadata["title"], "Episode")
        self.assertEqual(metadata["show_title"], "Series")


if __name__ == "__main__":
    unittest.main()
