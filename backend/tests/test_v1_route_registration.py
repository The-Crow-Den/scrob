import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from main import app


class NativeRouteRegistrationTests(unittest.TestCase):
    def test_required_velum_routes_are_registered(self):
        routes = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }

        required = {
            ("/v1/playback/sessions", "POST"),
            ("/v1/playback/sessions/active", "GET"),
            ("/v1/playback/sessions/{session_id}/events", "POST"),
            ("/v1/history/resolve", "POST"),
            ("/v1/history/continue-watching", "GET"),
            ("/v1/history/next-up", "GET"),
            ("/v1/history/items/{media_key}", "GET"),
            ("/v1/history/items/{media_key}", "PUT"),
            ("/v1/history/items/{media_key}", "DELETE"),
        }

        missing = sorted(required - routes)
        self.assertEqual(missing, [], f"Missing native routes: {missing}")


if __name__ == "__main__":
    unittest.main()
