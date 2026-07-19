"""Tests for the FastMCP list server, exercised via the in-memory Client.

Uses a real temp-SQLite DB (schema not mocked) and calls the tools exactly as an
MCP client would — proving the server object + tool schemas + DB path all work.
Skipped automatically if fastmcp is not installed.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastmcp import Client
    _HAS_FASTMCP = True
except ImportError:  # pragma: no cover - env without fastmcp
    _HAS_FASTMCP = False


@unittest.skipUnless(_HAS_FASTMCP, "fastmcp not installed")
class TestMcpListServer(unittest.IsolatedAsyncioTestCase):
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
        DatabaseManager()  # create schema

    def tearDown(self):
        import api_service.db.database_manager as dm_mod
        dm_mod.DatabaseManager._instance = None
        self._path_patch.stop()
        self._env_patch.stop()
        try:
            os.unlink(self._db_file)
        except FileNotFoundError:
            pass

    def _client(self):
        from api_service.mcp_server.list_server import mcp
        return Client(mcp)

    async def test_tools_are_exposed(self):
        async with self._client() as client:
            names = {t.name for t in await client.list_tools()}
        self.assertEqual(
            {"add_list", "list_lists", "remove_list", "sync_list"} & names,
            {"add_list", "list_lists", "remove_list", "sync_list"},
        )

    async def test_add_list_autodetects_source_and_lists(self):
        async with self._client() as client:
            res = await client.call_tool(
                "add_list", {"url": "https://trakt.tv/users/x/lists/best-of-2024"}
            )
            created = res.data
            self.assertEqual(created["source"], "trakt")
            self.assertEqual(created["name"], "Best Of 2024")  # derived from slug
            self.assertTrue(created["collection_enabled"])

            listed = (await client.call_tool("list_lists", {})).data
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["url"], "https://trakt.tv/users/x/lists/best-of-2024")

    async def test_add_list_letterboxd_and_remove(self):
        async with self._client() as client:
            created = (await client.call_tool("add_list", {
                "url": "https://letterboxd.com/dave/list/imdb-top-250/",
                "collection": False, "media_type": "movie",
            })).data
            self.assertEqual(created["source"], "letterboxd")
            self.assertFalse(created["collection_enabled"])

            removed = (await client.call_tool(
                "remove_list", {"list_id": created["id"]})).data
            self.assertTrue(removed["removed"])
            self.assertEqual((await client.call_tool("list_lists", {})).data, [])

    async def test_add_list_unknown_source_errors(self):
        async with self._client() as client:
            result = await client.call_tool(
                "add_list", {"url": "https://imdb.com/list/xyz"},
                raise_on_error=False,
            )
            self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()
