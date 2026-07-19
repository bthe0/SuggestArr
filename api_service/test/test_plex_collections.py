"""Unit tests for Plex collection support on PlexLibraryService.

The Plex HTTP layer (``_make_request``) is mocked — these tests never touch a
real Plex server (the running Plex is part of the media stack and must not be
mutated).  A base-client test covers method routing + empty-body handling.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from api_service.services.plex.library_service import PlexLibraryService


def _svc():
    s = PlexLibraryService(token="tok", api_url="http://plex:32400")
    return s


def _page(items, total):
    return {"MediaContainer": {"totalSize": total, "size": len(items), "Metadata": items}}


def _item(rating_key, tmdb):
    return {"ratingKey": rating_key, "Guid": [
        {"id": "imdb://tt1"}, {"id": f"tmdb://{tmdb}"}]}


class TestBuildRatingKeyMap(unittest.IsolatedAsyncioTestCase):
    async def test_maps_tmdb_to_rating_key_across_pages(self):
        svc = _svc()
        calls = []

        async def fake(url, method="GET"):
            calls.append(url)
            if "X-Plex-Container-Start=0" in url:
                return _page([_item("100", "278"), _item("101", "238")], total=3)
            return _page([_item("102", "155")], total=3)

        svc._make_request = AsyncMock(side_effect=fake)
        mapping = await svc.build_tmdb_rating_key_map("5")
        self.assertEqual(mapping, {"278": "100", "238": "101", "155": "102"})
        self.assertEqual(len(calls), 2)  # paginated

    async def test_skips_items_without_tmdb_guid(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value=_page(
            [{"ratingKey": "9", "Guid": [{"id": "imdb://tt9"}]}], total=1))
        self.assertEqual(await svc.build_tmdb_rating_key_map("5"), {})

    async def test_empty_section(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value=_page([], total=0))
        self.assertEqual(await svc.build_tmdb_rating_key_map("5"), {})


class TestFindCollection(unittest.IsolatedAsyncioTestCase):
    async def test_found(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value={"MediaContainer": {"Metadata": [
            {"title": "Other", "ratingKey": "1"},
            {"title": "My List", "ratingKey": "42"},
        ]}})
        self.assertEqual(await svc.find_collection("5", "My List"),
                         {"ratingKey": "42", "title": "My List"})

    async def test_not_found(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value={"MediaContainer": {"Metadata": []}})
        self.assertIsNone(await svc.find_collection("5", "My List"))


class TestCreateAddDelete(unittest.IsolatedAsyncioTestCase):
    async def test_create_collection_builds_correct_request(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value={"MediaContainer": {"Metadata": [
            {"ratingKey": "500", "title": "My List"}]}})
        cid = await svc.create_collection("5", "My List", ["100", "101"], "MACHINE", "movie")
        self.assertEqual(cid, "500")
        url, kwargs = svc._make_request.call_args.args[0], svc._make_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertIn("type=1", url)
        self.assertIn("sectionId=5", url)
        self.assertIn("title=My%20List", url)
        # uri is percent-encoded and references the ratingKeys + machine id
        self.assertIn("MACHINE", url)
        self.assertIn("100%2C101", url)  # "100,101" encoded

    async def test_create_tv_uses_type_2(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value={"MediaContainer": {"Metadata": [
            {"ratingKey": "7"}]}})
        await svc.create_collection("8", "TV", ["1"], "M", "tv")
        self.assertIn("type=2", svc._make_request.call_args.args[0])

    async def test_create_empty_rating_keys_noop(self):
        svc = _svc()
        svc._make_request = AsyncMock()
        self.assertIsNone(await svc.create_collection("5", "T", [], "M"))
        svc._make_request.assert_not_called()

    async def test_add_to_collection(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value={})
        ok = await svc.add_to_collection("500", ["102"], "MACHINE")
        self.assertTrue(ok)
        url, kwargs = svc._make_request.call_args.args[0], svc._make_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "PUT")
        self.assertIn("/library/collections/500/items", url)

    async def test_delete_collection(self):
        svc = _svc()
        svc._make_request = AsyncMock(return_value={})
        self.assertTrue(await svc.delete_collection("500"))
        url, kwargs = svc._make_request.call_args.args[0], svc._make_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "DELETE")
        self.assertTrue(url.endswith("/library/collections/500"))


class TestSyncCollection(unittest.IsolatedAsyncioTestCase):
    def _wire(self, svc, mapping, existing=None):
        svc.build_tmdb_rating_key_map = AsyncMock(return_value=mapping)
        svc.get_machine_identifier = AsyncMock(return_value="MACHINE")
        svc.find_collection = AsyncMock(return_value=existing)
        svc.create_collection = AsyncMock(return_value="500")
        svc.add_to_collection = AsyncMock(return_value=True)

    async def test_creates_when_absent(self):
        svc = _svc()
        self._wire(svc, {"278": "100", "238": "101"}, existing=None)
        res = await svc.sync_collection("5", "My List", ["278", "238", "999"], "movie")
        self.assertEqual(res["collection_id"], "500")
        self.assertTrue(res["created"])
        self.assertEqual(res["matched"], 2)
        self.assertEqual(res["missing"], ["999"])
        svc.create_collection.assert_awaited_once()
        svc.add_to_collection.assert_not_awaited()

    async def test_adds_when_existing(self):
        svc = _svc()
        self._wire(svc, {"278": "100"}, existing={"ratingKey": "42", "title": "My List"})
        res = await svc.sync_collection("5", "My List", ["278"], "movie")
        self.assertEqual(res["collection_id"], "42")
        self.assertFalse(res["created"])
        svc.add_to_collection.assert_awaited_once()
        svc.create_collection.assert_not_awaited()

    async def test_no_matches_skips_creation(self):
        svc = _svc()
        self._wire(svc, {}, existing=None)
        res = await svc.sync_collection("5", "My List", ["999"], "movie")
        self.assertIsNone(res["collection_id"])
        self.assertEqual(res["matched"], 0)
        svc.create_collection.assert_not_awaited()


class TestMakeRequestMethodRouting(unittest.IsolatedAsyncioTestCase):
    def _session(self, status, body):
        resp = AsyncMock()
        resp.status = status
        resp.reason = "OK"
        resp.text = AsyncMock(return_value=body)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.request = MagicMock(return_value=resp)
        return session, resp

    async def test_post_empty_body_returns_empty_dict(self):
        svc = _svc()
        session, _ = self._session(201, "")
        svc._get_session = AsyncMock(return_value=session)
        result = await svc._make_request("http://plex/x", method="POST")
        self.assertEqual(result, {})
        self.assertEqual(session.request.call_args.args[0], "POST")

    async def test_get_json_body_parsed(self):
        svc = _svc()
        session, _ = self._session(200, '{"MediaContainer": {"size": 1}}')
        svc._get_session = AsyncMock(return_value=session)
        result = await svc._make_request("http://plex/x")
        self.assertEqual(result["MediaContainer"]["size"], 1)

    async def test_non_2xx_raises(self):
        from api_service.exceptions.api_exceptions import PlexConnectionError
        svc = _svc()
        session, _ = self._session(404, "")
        svc._get_session = AsyncMock(return_value=session)
        with self.assertRaises(PlexConnectionError):
            await svc._make_request("http://plex/x")


if __name__ == "__main__":
    unittest.main()
