"""Integration tests for ListService orchestration.

Uses a real temp-SQLite ListRepository (persistence not mocked) while patching
the HTTP clients and the ListSyncService engine, to verify that sync_list:
  - wires the repo's already-requested dedup set into the sync engine,
  - builds the correct source client (Trakt vs Letterboxd + FlareSolverr URL),
  - persists membership + requested items and stamps last-sync status,
  - marks status='error' and re-raises on ListFetchError.
"""
import logging
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

logging.disable(logging.CRITICAL)

_MOD = "api_service.services.lists.list_service"


class _FakeClient:
    """Async-context-manager stand-in for the HTTP clients."""
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ServiceBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import api_service.db.database_manager as dm_mod
        dm_mod.DatabaseManager._instance = None
        fd, self._db_file = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._path_patch = patch.object(dm_mod, "DB_PATH", self._db_file)
        self._path_patch.start()
        self._env_patch = patch(
            "api_service.db.database_manager.load_env_vars",
            return_value={"DB_TYPE": "sqlite"},
        )
        self._env_patch.start()

        from api_service.db.database_manager import DatabaseManager
        DatabaseManager()
        from api_service.db.list_repository import ListRepository
        from api_service.services.lists.list_service import ListService
        self.repo = ListRepository()
        self.service = ListService(repository=self.repo)

    def tearDown(self):
        import api_service.db.database_manager as dm_mod
        dm_mod.DatabaseManager._instance = None
        self._path_patch.stop()
        self._env_patch.stop()
        try:
            os.unlink(self._db_file)
        except FileNotFoundError:
            pass

    def _make(self, **over):
        data = {"name": "L", "source": "trakt", "url": "https://trakt.tv/lists/1"}
        data.update(over)
        return self.repo.create_list(data)

    def _summary(self):
        return {
            "name": "L", "source": "trakt", "url": "u",
            "fetched": 2, "requested": 1, "skipped_dedup": 1,
            "media_ids": [
                {"tmdb_id": "603", "media_type": "movie"},
                {"tmdb_id": "500", "media_type": "movie"},
            ],
            "requested_ids": [{"tmdb_id": "500", "media_type": "movie"}],
        }


_ENV = {
    "SEER_API_URL": "http://seer", "SEER_TOKEN": "t",
    "SEER_USER_NAME": None, "SEER_USER_PSW": None, "SEER_SESSION_TOKEN": None,
    "TMDB_API_KEY": "k", "TRAKT_CLIENT_ID": "trakt-key",
    "FLARESOLVERR_URL": "http://flaresolverr:8191/v1",
}


class _FakePlex:
    """Async-context-manager stand-in for PlexLibraryService."""
    def __init__(self, sync_result=None, raise_on_sync=False):
        self.sync_result = sync_result or {
            "collection_id": "500", "matched": 1, "missing": [], "created": True,
        }
        self.raise_on_sync = raise_on_sync
        self.deleted: list[str] = []
        self.synced = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def filter_libraries_by_type(self, media_type):
        return [{"key": "5", "type": "movie"}]

    async def sync_collection(self, section_id, title, tmdb_ids, media_type):
        if self.raise_on_sync:
            raise RuntimeError("plex boom")
        self.synced = (section_id, title, list(tmdb_ids), media_type)
        return self.sync_result

    async def delete_collection(self, collection_id):
        self.deleted.append(collection_id)
        return True


class TestCollectionWiring(_ServiceBase):
    async def _run_sync(self, plex, collection_enabled=True):
        list_id = self._make(collection_enabled=collection_enabled)
        fake_sync = MagicMock()
        fake_sync.sync_list = AsyncMock(return_value=self._summary())
        self.service._build_plex = lambda env: plex
        with patch(f"{_MOD}.load_env_vars", return_value=_ENV), \
                patch(f"{_MOD}.SeerClient", _FakeClient), \
                patch(f"{_MOD}.TraktListClient", _FakeClient), \
                patch(f"{_MOD}.ListSyncService", return_value=fake_sync):
            summary = await self.service.sync_list(list_id)
        return list_id, summary

    async def test_builds_collection_and_persists_id(self):
        plex = _FakePlex()
        list_id, summary = await self._run_sync(plex)
        # only movie tmdb ids from media_ids are sent (603, 500)
        self.assertEqual(plex.synced[1], "L")            # collection named after list
        self.assertEqual(sorted(plex.synced[2]), ["500", "603"])
        self.assertEqual(summary["collection"]["collection_id"], "500")
        self.assertEqual(self.repo.get_list(list_id)["collection_id"], "500")

    async def test_no_collection_when_disabled(self):
        plex = _FakePlex()
        list_id, summary = await self._run_sync(plex, collection_enabled=False)
        self.assertIsNone(summary["collection"])
        self.assertIsNone(plex.synced)
        self.assertIsNone(self.repo.get_list(list_id)["collection_id"])

    async def test_collection_error_does_not_fail_sync(self):
        plex = _FakePlex(raise_on_sync=True)
        list_id, summary = await self._run_sync(plex)
        self.assertIn("error", summary["collection"])
        # sync itself still succeeded and persisted requests
        self.assertEqual(self.repo.get_list(list_id)["last_status"], "success")
        # _summary() marks only tmdb 500 as requested (603 is membership-only).
        self.assertEqual(self.repo.get_requested_keys(list_id), {("movie", "500")})


class TestRemove(_ServiceBase):
    async def test_remove_deletes_collection_and_row(self):
        list_id = self._make()
        self.repo.update_list(list_id, {"collection_id": "500"})
        plex = _FakePlex()
        self.service._build_plex = lambda env: plex
        with patch(f"{_MOD}.load_env_vars", return_value=_ENV):
            removed = await self.service.remove(list_id)
        self.assertTrue(removed)
        self.assertEqual(plex.deleted, ["500"])
        self.assertIsNone(self.repo.get_list(list_id))

    async def test_remove_without_collection_skips_plex(self):
        list_id = self._make()
        plex = _FakePlex()
        self.service._build_plex = lambda env: plex
        with patch(f"{_MOD}.load_env_vars", return_value=_ENV):
            removed = await self.service.remove(list_id)
        self.assertTrue(removed)
        self.assertEqual(plex.deleted, [])

    async def test_remove_missing_returns_false(self):
        with patch(f"{_MOD}.load_env_vars", return_value=_ENV):
            self.assertFalse(await self.service.remove(9999))

    async def test_remove_collection_delete_failure_still_removes_row(self):
        list_id = self._make()
        self.repo.update_list(list_id, {"collection_id": "500"})
        plex = _FakePlex()
        plex.delete_collection = AsyncMock(side_effect=RuntimeError("boom"))
        self.service._build_plex = lambda env: plex
        with patch(f"{_MOD}.load_env_vars", return_value=_ENV):
            removed = await self.service.remove(list_id)
        self.assertTrue(removed)
        self.assertIsNone(self.repo.get_list(list_id))


class TestSyncOrchestration(_ServiceBase):
    async def test_persists_and_wires_dedup(self):
        list_id = self._make()
        # Pre-seed a previously-requested item so we can assert it is passed in.
        self.repo.record_sync_items(
            list_id, [{"tmdb_id": "603", "media_type": "movie"}],
            [{"tmdb_id": "603", "media_type": "movie"}],
        )
        fake_sync = MagicMock()
        fake_sync.sync_list = AsyncMock(return_value=self._summary())

        with patch(f"{_MOD}.load_env_vars", return_value=_ENV), \
                patch(f"{_MOD}.SeerClient", _FakeClient), \
                patch(f"{_MOD}.TraktListClient", _FakeClient), \
                patch(f"{_MOD}.ListSyncService", return_value=fake_sync):
            summary = await self.service.sync_list(list_id)

        # dedup set from the repo was handed to the engine
        passed_already = fake_sync.sync_list.call_args.args[1]
        self.assertEqual(passed_already, {("movie", "603")})

        # newly-requested item persisted; membership recorded
        keys = self.repo.get_requested_keys(list_id)
        self.assertEqual(keys, {("movie", "603"), ("movie", "500")})

        row = self.repo.get_list(list_id)
        self.assertEqual(row["last_status"], "success")
        self.assertEqual(row["last_summary"]["requested"], 1)
        self.assertIsNotNone(row["last_synced_at"])
        self.assertEqual(summary["requested"], 1)

    async def test_letterboxd_builds_client_with_flaresolverr(self):
        list_id = self._make(source="letterboxd",
                            url="https://letterboxd.com/u/list/s/")
        fake_sync = MagicMock()
        fake_sync.sync_list = AsyncMock(return_value=self._summary())
        lb_ctor = MagicMock(return_value=_FakeClient())

        with patch(f"{_MOD}.load_env_vars", return_value=_ENV), \
                patch(f"{_MOD}.SeerClient", _FakeClient), \
                patch(f"{_MOD}.TMDbClient", _FakeClient), \
                patch(f"{_MOD}.LetterboxdListClient", lb_ctor), \
                patch(f"{_MOD}.ListSyncService", return_value=fake_sync):
            await self.service.sync_list(list_id)

        self.assertEqual(
            lb_ctor.call_args.kwargs["flaresolverr_url"],
            "http://flaresolverr:8191/v1",
        )

    async def test_error_marks_status_and_reraises(self):
        from api_service.exceptions.api_exceptions import ListFetchError
        list_id = self._make()
        fake_sync = MagicMock()
        fake_sync.sync_list = AsyncMock(side_effect=ListFetchError("down", "Trakt"))

        with patch(f"{_MOD}.load_env_vars", return_value=_ENV), \
                patch(f"{_MOD}.SeerClient", _FakeClient), \
                patch(f"{_MOD}.TraktListClient", _FakeClient), \
                patch(f"{_MOD}.ListSyncService", return_value=fake_sync):
            with self.assertRaises(ListFetchError):
                await self.service.sync_list(list_id)

        row = self.repo.get_list(list_id)
        self.assertEqual(row["last_status"], "error")

    async def test_unknown_list_raises_value_error(self):
        with patch(f"{_MOD}.load_env_vars", return_value=_ENV):
            with self.assertRaises(ValueError):
                await self.service.sync_list(4242)


if __name__ == "__main__":
    unittest.main()
