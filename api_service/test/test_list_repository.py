"""Integration tests for ListRepository against a real temp SQLite database.

Strategy mirrors test_db_users.py: reset the DatabaseManager singleton and point
DB_PATH at a fresh temp file per test, so the real schema (monitored_lists +
monitored_list_items) is exercised — the ORM/DB layer is not mocked.
"""
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

logging.disable(logging.CRITICAL)


class _RepoBase(unittest.TestCase):
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
        DatabaseManager()  # triggers initialize_db -> creates tables

        from api_service.db.list_repository import ListRepository
        self.repo = ListRepository()

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
        data = {
            "name": "My List",
            "source": "trakt",
            "url": "https://trakt.tv/users/x/lists/y",
        }
        data.update(over)
        return self.repo.create_list(data)


class TestListCrud(_RepoBase):
    def test_create_and_get(self):
        list_id = self._make(media_type="movie", quality="1080p",
                             collection_enabled=False, sync_interval_hours=6)
        row = self.repo.get_list(list_id)
        self.assertEqual(row["name"], "My List")
        self.assertEqual(row["source"], "trakt")
        self.assertEqual(row["media_type"], "movie")
        self.assertEqual(row["quality"], "1080p")
        self.assertFalse(row["collection_enabled"])
        self.assertTrue(row["enabled"])
        self.assertEqual(row["sync_interval_hours"], 6)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.repo.get_list(999))

    def test_defaults_applied(self):
        row = self.repo.get_list(self._make())
        self.assertTrue(row["collection_enabled"])
        self.assertEqual(row["sync_interval_hours"], 24)
        self.assertIsNone(row["media_type"])

    def test_get_all_and_enabled(self):
        a = self._make(url="https://trakt.tv/lists/1")
        b = self._make(url="https://trakt.tv/lists/2")
        self.repo.update_list(b, {"enabled": False})
        self.assertEqual(len(self.repo.get_all_lists()), 2)
        enabled = self.repo.get_enabled_lists()
        self.assertEqual([r["id"] for r in enabled], [a])

    def test_unique_source_url(self):
        self._make(url="https://trakt.tv/lists/dupe")
        with self.assertRaises(Exception):
            self._make(url="https://trakt.tv/lists/dupe")

    def test_invalid_source_rejected(self):
        with self.assertRaises(ValueError):
            self.repo.create_list({"name": "n", "source": "imdb", "url": "u"})

    def test_missing_fields_rejected(self):
        with self.assertRaises(ValueError):
            self.repo.create_list({"name": "", "source": "trakt", "url": "u"})

    def test_update_whitelist_and_unknown_ignored(self):
        list_id = self._make()
        ok = self.repo.update_list(list_id, {
            "last_status": "success",
            "collection_id": "col-42",
            "bogus": "nope",  # ignored, not an error
        })
        self.assertTrue(ok)
        row = self.repo.get_list(list_id)
        self.assertEqual(row["last_status"], "success")
        self.assertEqual(row["collection_id"], "col-42")
        self.assertNotIn("bogus", row)

    def test_update_no_fields_returns_false(self):
        list_id = self._make()
        self.assertFalse(self.repo.update_list(list_id, {"bogus": 1}))

    def test_last_summary_json_roundtrip(self):
        list_id = self._make()
        self.repo.update_list(list_id, {"last_summary": json.dumps({"requested": 3})})
        row = self.repo.get_list(list_id)
        self.assertEqual(row["last_summary"], {"requested": 3})

    def test_delete(self):
        list_id = self._make()
        self.assertTrue(self.repo.delete_list(list_id))
        self.assertIsNone(self.repo.get_list(list_id))
        self.assertFalse(self.repo.delete_list(list_id))


class TestListItems(_RepoBase):
    def test_record_and_get_requested_keys(self):
        list_id = self._make()
        members = [
            {"tmdb_id": "1", "media_type": "movie"},
            {"tmdb_id": "2", "media_type": "movie"},
            {"tmdb_id": "3", "media_type": "tv"},
        ]
        requested = [{"tmdb_id": "1", "media_type": "movie"},
                     {"tmdb_id": "3", "media_type": "tv"}]
        self.repo.record_sync_items(list_id, members, requested)

        keys = self.repo.get_requested_keys(list_id)
        self.assertEqual(keys, {("movie", "1"), ("tv", "3")})

        members_out = self.repo.get_member_items(list_id)
        self.assertEqual(len(members_out), 3)  # all recorded, incl. non-requested

    def test_record_is_idempotent(self):
        list_id = self._make()
        members = [{"tmdb_id": "1", "media_type": "movie"}]
        self.repo.record_sync_items(list_id, members, [])
        self.repo.record_sync_items(list_id, members, members)  # re-run marks requested
        self.assertEqual(len(self.repo.get_member_items(list_id)), 1)
        self.assertEqual(self.repo.get_requested_keys(list_id), {("movie", "1")})

    def test_requested_only_in_requested_list_not_marked(self):
        list_id = self._make()
        # An item present only in `requested` is still recorded and marked.
        self.repo.record_sync_items(list_id, [], [{"tmdb_id": "9", "media_type": "movie"}])
        self.assertEqual(self.repo.get_requested_keys(list_id), {("movie", "9")})

    def test_empty_record_noop(self):
        list_id = self._make()
        self.repo.record_sync_items(list_id, [], [])
        self.assertEqual(self.repo.get_member_items(list_id), [])

    def test_items_isolated_per_list(self):
        a = self._make(url="https://trakt.tv/lists/a")
        b = self._make(url="https://trakt.tv/lists/b")
        self.repo.record_sync_items(a, [{"tmdb_id": "1", "media_type": "movie"}],
                                    [{"tmdb_id": "1", "media_type": "movie"}])
        self.assertEqual(self.repo.get_requested_keys(a), {("movie", "1")})
        self.assertEqual(self.repo.get_requested_keys(b), set())

    def test_new_list_has_no_leaked_items(self):
        list_id = self._make()
        self.repo.record_sync_items(list_id, [{"tmdb_id": "1", "media_type": "movie"}], [])
        self.repo.delete_list(list_id)
        # A freshly-created list (new id) must start with an empty item set.
        new_id = self._make()
        self.assertEqual(self.repo.get_member_items(new_id), [])


if __name__ == "__main__":
    unittest.main()
