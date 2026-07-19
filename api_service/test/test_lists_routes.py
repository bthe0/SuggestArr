"""Route tests for the lists blueprint (Flask test client).

Mirrors test_plex_routes.py: a bare Flask app with just the blueprint registered
(so the global auth middleware is not attached).  An authenticated admin is
injected via before_request so the real ``@require_role('admin')`` decorator
passes — auth/authz itself is covered by the auth test-suite; here we test
routing, validation, and delegation to a mocked ListService.
"""
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask, g

from api_service.blueprints.lists.routes import lists_bp
from api_service.exceptions.api_exceptions import ListFetchError

_ROUTES = "api_service.blueprints.lists.routes"


class _RouteBase(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.before_request
        def _inject_admin():
            g.current_user = {"id": 1, "role": "admin"}

        app.register_blueprint(lists_bp, url_prefix="/api/lists")
        self.client = app.test_client()
        self._svc = MagicMock()
        self._patch = patch(f"{_ROUTES}.ListService", return_value=self._svc)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()


class TestGetLists(_RouteBase):
    def test_returns_lists(self):
        self._svc.list_all.return_value = [{"id": 1, "name": "L"}]
        resp = self.client.get("/api/lists")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["lists"], [{"id": 1, "name": "L"}])


class TestAddList(_RouteBase):
    def test_create_ok(self):
        self._svc.create.return_value = {"id": 7, "name": "Faves", "source": "trakt"}
        resp = self.client.post("/api/lists", json={
            "name": "Faves", "source": "trakt",
            "url": "https://trakt.tv/users/x/lists/y",
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["list"]["id"], 7)
        cleaned = self._svc.create.call_args.args[0]
        self.assertEqual(cleaned["source"], "trakt")
        self.assertTrue(cleaned["collection_enabled"])
        self.assertEqual(cleaned["sync_interval_hours"], 24)

    def test_missing_name_400(self):
        resp = self.client.post("/api/lists", json={"source": "trakt", "url": "u"})
        self.assertEqual(resp.status_code, 400)
        self._svc.create.assert_not_called()

    def test_invalid_source_400(self):
        resp = self.client.post("/api/lists", json={
            "name": "n", "source": "imdb", "url": "u"})
        self.assertEqual(resp.status_code, 400)

    def test_missing_url_400(self):
        resp = self.client.post("/api/lists", json={"name": "n", "source": "trakt"})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_media_type_400(self):
        resp = self.client.post("/api/lists", json={
            "name": "n", "source": "trakt", "url": "u", "media_type": "book"})
        self.assertEqual(resp.status_code, 400)

    def test_non_positive_interval_400(self):
        resp = self.client.post("/api/lists", json={
            "name": "n", "source": "trakt", "url": "u", "sync_interval_hours": 0})
        self.assertEqual(resp.status_code, 400)

    def test_non_integer_interval_400(self):
        resp = self.client.post("/api/lists", json={
            "name": "n", "source": "trakt", "url": "u", "sync_interval_hours": "soon"})
        self.assertEqual(resp.status_code, 400)

    def test_letterboxd_source_normalised(self):
        self._svc.create.return_value = {"id": 1}
        resp = self.client.post("/api/lists", json={
            "name": "n", "source": "Letterboxd",
            "url": "https://letterboxd.com/u/list/s/", "media_type": "movie"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self._svc.create.call_args.args[0]["source"], "letterboxd")


class TestRemoveList(_RouteBase):
    def _run_patch(self, **kw):
        return patch(f"{_ROUTES}.run_coroutine_sync", MagicMock(**kw))

    def test_delete_ok(self):
        with self._run_patch(return_value=True):
            resp = self.client.delete("/api/lists/3")
        self.assertEqual(resp.status_code, 200)

    def test_delete_missing_404(self):
        with self._run_patch(return_value=False):
            resp = self.client.delete("/api/lists/99")
        self.assertEqual(resp.status_code, 404)


class TestSyncNow(_RouteBase):
    def _run_patch(self, **kw):
        return patch(f"{_ROUTES}.run_coroutine_sync", MagicMock(**kw))

    def test_sync_ok(self):
        summary = {"requested": 5, "fetched": 10}
        with self._run_patch(return_value=summary):
            resp = self.client.post("/api/lists/2/sync")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["summary"], summary)

    def test_sync_not_found_404(self):
        with self._run_patch(side_effect=ValueError("Monitored list 2 not found.")):
            resp = self.client.post("/api/lists/2/sync")
        self.assertEqual(resp.status_code, 404)

    def test_sync_fetch_error_502(self):
        with self._run_patch(side_effect=ListFetchError("trakt down", "Trakt")):
            resp = self.client.post("/api/lists/2/sync")
        self.assertEqual(resp.status_code, 502)


if __name__ == "__main__":
    unittest.main()
