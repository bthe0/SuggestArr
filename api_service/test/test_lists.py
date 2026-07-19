"""Unit tests for the monitored-list ingestion services (increment 1).

Covers:
- TraktListClient: URL parsing, item extraction/skip rules, pagination, dedup,
  auth/404/network error handling.
- LetterboxdListClient: RSS url building, resolution via direct tmdb:movieId,
  via letterboxd:filmTitle/Year, via "<title>Name, Year</title>", TMDB-miss skip,
  Cloudflare-challenge / HTTP-error / bad-XML handling.
- ListSyncService: happy path, dedup, media_type filter, max_items cap,
  unsupported/unconfigured source, seer-returns-False, per-item error isolation,
  and that request_media is called with source=None + a rationale carrying the
  list name.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from api_service.services.lists.trakt import TraktListClient
from api_service.services.lists.letterboxd import LetterboxdListClient
from api_service.services.lists.list_sync import ListSyncService
from api_service.exceptions.api_exceptions import ListFetchError


# ---------------------------------------------------------------------------
# Shared HTTP mock helpers (mirrors test_tmdb_client.py conventions)
# ---------------------------------------------------------------------------

def _mock_json_response(status=200, data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data if data is not None else [])
    resp.text = AsyncMock(return_value="")
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _session_returning(*responses):
    """Return a mock session whose .get() yields the given responses in order."""
    session = MagicMock()
    session.get = MagicMock(side_effect=list(responses))
    return session


def _trakt_movie(tmdb_id):
    return {"type": "movie", "movie": {"title": "M", "year": 2000, "ids": {"tmdb": tmdb_id}}}


def _trakt_show(tmdb_id):
    return {"type": "show", "show": {"title": "S", "year": 2001, "ids": {"tmdb": tmdb_id}}}


# ---------------------------------------------------------------------------
# TraktListClient
# ---------------------------------------------------------------------------

class TestTraktUrlParsing(unittest.TestCase):
    def test_user_list_url(self):
        self.assertEqual(
            TraktListClient.parse_list_url("https://trakt.tv/users/theo/lists/best-of-2024"),
            "users/theo/lists/best-of-2024/items",
        )

    def test_user_list_url_with_extra_segments(self):
        self.assertEqual(
            TraktListClient.parse_list_url("https://trakt.tv/users/theo/lists/best/by/rank/asc"),
            "users/theo/lists/best/items",
        )

    def test_watchlist_url(self):
        self.assertEqual(
            TraktListClient.parse_list_url("https://trakt.tv/users/theo/watchlist"),
            "users/theo/watchlist",
        )

    def test_standalone_list_url(self):
        self.assertEqual(
            TraktListClient.parse_list_url("https://trakt.tv/lists/1234567"),
            "lists/1234567/items",
        )

    def test_bare_path(self):
        self.assertEqual(
            TraktListClient.parse_list_url("users/theo/lists/best"),
            "users/theo/lists/best/items",
        )

    def test_invalid_url_raises(self):
        with self.assertRaises(ListFetchError):
            TraktListClient.parse_list_url("https://trakt.tv/movies/popular")

    def test_empty_url_raises(self):
        with self.assertRaises(ListFetchError):
            TraktListClient.parse_list_url("")


class TestTraktFetch(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_movies_and_shows_skips_others(self):
        client = TraktListClient("key")
        page = [
            _trakt_movie(603),
            _trakt_show(1396),
            {"type": "episode", "episode": {"ids": {"tmdb": 999}}},   # skipped
            {"type": "person", "person": {"ids": {"tmdb": 55}}},      # skipped
            {"type": "movie", "movie": {"ids": {"imdb": "tt1"}}},     # no tmdb -> skip
        ]
        client._get_session = AsyncMock(return_value=_session_returning(_mock_json_response(200, page)))
        result = await client.fetch_list("https://trakt.tv/users/x/lists/y")
        self.assertEqual(result, [("603", "movie"), ("1396", "tv")])

    async def test_sends_required_headers(self):
        client = TraktListClient("secret-key")
        session = _session_returning(_mock_json_response(200, [_trakt_movie(1)]))
        client._get_session = AsyncMock(return_value=session)
        await client.fetch_list("https://trakt.tv/lists/1")
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["headers"]["trakt-api-key"], "secret-key")
        self.assertEqual(kwargs["headers"]["trakt-api-version"], "2")

    async def test_pagination_walks_until_short_page(self):
        client = TraktListClient("key")
        full_page = [_trakt_movie(1000 + i) for i in range(100)]  # exactly _PER_PAGE
        second_page = [_trakt_movie(2000), _trakt_movie(1000)]    # 1000 is a dup
        session = _session_returning(
            _mock_json_response(200, full_page),
            _mock_json_response(200, second_page),
        )
        client._get_session = AsyncMock(return_value=session)
        result = await client.fetch_list("https://trakt.tv/lists/1")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(result), 101)          # 100 + 1 new (dup dropped)
        self.assertIn(("2000", "movie"), result)

    async def test_missing_client_id_raises(self):
        client = TraktListClient("")
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://trakt.tv/lists/1")

    async def test_401_raises(self):
        client = TraktListClient("bad")
        client._get_session = AsyncMock(return_value=_session_returning(_mock_json_response(401)))
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://trakt.tv/lists/1")

    async def test_404_raises(self):
        client = TraktListClient("key")
        client._get_session = AsyncMock(return_value=_session_returning(_mock_json_response(404)))
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://trakt.tv/lists/1")

    async def test_network_error_raises(self):
        client = TraktListClient("key")
        session = MagicMock()
        session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
        client._get_session = AsyncMock(return_value=session)
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://trakt.tv/lists/1")


# ---------------------------------------------------------------------------
# LetterboxdListClient
# ---------------------------------------------------------------------------

# A trimmed slice of a real Letterboxd list page: each film is a LazyPoster
# react-component carrying data-item-name="Title (YYYY)" (no embedded TMDB id).
def _poster(name, slug):
    return (
        '<li class="poster-container"><div class="react-component" '
        'data-component-class="LazyPoster" '
        f'data-item-name="{name}" data-item-slug="{slug}" '
        f'data-item-link="/film/{slug}/"></div></li>'
    )


_LIST_HTML = (
    "<html><head><title>My List, a list of films by X • Letterboxd</title>"
    "</head><body><ul class=\"poster-list\">"
    + _poster("Parasite (2019)", "parasite")
    + _poster("The Matrix (1999)", "the-matrix")
    + _poster("Fast &amp; Furious (2009)", "fast-and-furious")
    + _poster("No Such Film (1900)", "no-such-film")
    + "</ul></body></html>"
)

_NOTFOUND_HTML = "<html><head><title>Letterboxd - Not Found</title></head><body></body></html>"


def _tmdb_search_stub():
    async def _search(title, year=None):
        table = {
            "Parasite": [{"id": 496243}],
            "The Matrix": [{"id": 603}],
            "Fast & Furious": [{"id": 13804}],
        }
        return table.get(title, [])
    mock = MagicMock()
    mock.search_movie = AsyncMock(side_effect=_search)
    return mock


def _fs_envelope(response_html, status="ok", http_status=200):
    """Build a FlareSolverr /v1 response envelope."""
    return {
        "status": status,
        "message": "Challenge solved!",
        "solution": {"status": http_status, "response": response_html},
    }


def _session_post_returning(*responses):
    """Return a mock session whose .post() yields the given responses in order."""
    session = MagicMock()
    session.post = MagicMock(side_effect=list(responses))
    return session


def _lb_client(flaresolverr_url="http://flaresolverr:8191/v1"):
    return LetterboxdListClient(_tmdb_search_stub(), flaresolverr_url=flaresolverr_url)


class TestLetterboxdUrl(unittest.TestCase):
    def test_appends_slash(self):
        self.assertEqual(
            LetterboxdListClient.list_page_url("https://letterboxd.com/u/list/slug"),
            "https://letterboxd.com/u/list/slug/",
        )

    def test_strips_query_and_fragment(self):
        self.assertEqual(
            LetterboxdListClient.list_page_url("https://letterboxd.com/u/list/slug?p=2#x"),
            "https://letterboxd.com/u/list/slug/",
        )

    def test_strips_legacy_rss_suffix(self):
        self.assertEqual(
            LetterboxdListClient.list_page_url("https://letterboxd.com/u/list/slug/rss/"),
            "https://letterboxd.com/u/list/slug/",
        )


class TestLetterboxdParse(unittest.TestCase):
    def test_extracts_title_year_pairs_in_order(self):
        pairs = LetterboxdListClient._parse_item_names(_LIST_HTML)
        self.assertEqual(pairs, [
            ("Parasite", "2019"),
            ("The Matrix", "1999"),
            ("Fast & Furious", "2009"),   # HTML entity decoded
            ("No Such Film", "1900"),
        ])

    def test_dedups_repeated_items(self):
        html = _poster("Heat (1995)", "heat") + _poster("Heat (1995)", "heat")
        self.assertEqual(
            LetterboxdListClient._parse_item_names(html), [("Heat", "1995")]
        )

    def test_name_without_year_keeps_title(self):
        html = _poster("Untitled Doc", "untitled-doc")
        self.assertEqual(
            LetterboxdListClient._parse_item_names(html), [("Untitled Doc", None)]
        )

    def test_empty_html_returns_empty(self):
        self.assertEqual(LetterboxdListClient._parse_item_names("<html></html>"), [])


class TestLetterboxdResolve(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_via_tmdb_search(self):
        tmdb = _tmdb_search_stub()
        client = LetterboxdListClient(tmdb, flaresolverr_url="http://fs/v1")
        client._fetch_via_flaresolverr = AsyncMock(return_value=_LIST_HTML)
        result = await client.fetch_list("https://letterboxd.com/u/list/slug/")
        # 'No Such Film' has no TMDB match -> dropped.
        self.assertEqual(result, [
            ("496243", "movie"), ("603", "movie"), ("13804", "movie"),
        ])
        searched = [c.args[0] for c in tmdb.search_movie.call_args_list]
        self.assertIn("Parasite", searched)
        self.assertIn("Fast & Furious", searched)

    async def test_retries_search_without_year_on_miss(self):
        calls = []

        async def _search(title, year=None):
            calls.append((title, year))
            if title == "The Matrix" and year is None:
                return [{"id": 603}]
            return []

        tmdb = MagicMock()
        tmdb.search_movie = AsyncMock(side_effect=_search)
        client = LetterboxdListClient(tmdb, flaresolverr_url="http://fs/v1")
        client._fetch_via_flaresolverr = AsyncMock(
            return_value=_poster("The Matrix (1999)", "the-matrix")
        )
        result = await client.fetch_list("https://letterboxd.com/u/list/slug/")
        self.assertEqual(result, [("603", "movie")])
        self.assertIn(("The Matrix", "1999"), calls)
        self.assertIn(("The Matrix", None), calls)  # retried without year

    async def test_missing_flaresolverr_url_raises(self):
        client = LetterboxdListClient(_tmdb_search_stub(), flaresolverr_url=None)
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://letterboxd.com/u/list/slug/")

    async def test_flaresolverr_solves_and_parses(self):
        # Coordinator-requested: mock the FlareSolverr POST -> canned solved HTML.
        client = _lb_client()
        session = _session_post_returning(
            _mock_json_response(200, _fs_envelope(_LIST_HTML))
        )
        client._get_session = AsyncMock(return_value=session)
        result = await client.fetch_list("https://letterboxd.com/u/list/slug/")
        self.assertEqual(result[0], ("496243", "movie"))
        # Correct FlareSolverr command + list-page URL (not rss) are sent.
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "http://flaresolverr:8191/v1")
        self.assertEqual(kwargs["json"]["cmd"], "request.get")
        self.assertEqual(kwargs["json"]["url"], "https://letterboxd.com/u/list/slug/")

    async def test_not_found_page_raises(self):
        client = _lb_client()
        client._get_session = AsyncMock(return_value=_session_post_returning(
            _mock_json_response(200, _fs_envelope(_NOTFOUND_HTML))
        ))
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://letterboxd.com/u/list/gone/")

    async def test_no_films_parsed_raises(self):
        client = _lb_client()
        client._fetch_via_flaresolverr = AsyncMock(
            return_value="<html><body>valid but no posters</body></html>"
        )
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://letterboxd.com/u/list/slug/")

    async def test_flaresolverr_not_ok_raises(self):
        client = _lb_client()
        client._get_session = AsyncMock(return_value=_session_post_returning(
            _mock_json_response(200, _fs_envelope("", status="error"))
        ))
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://letterboxd.com/u/list/slug/")

    async def test_flaresolverr_http_non200_raises(self):
        client = _lb_client()
        client._get_session = AsyncMock(return_value=_session_post_returning(
            _mock_json_response(200, _fs_envelope(_LIST_HTML, http_status=404))
        ))
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://letterboxd.com/u/list/slug/")

    async def test_challenge_persists_raises(self):
        client = _lb_client()
        client._get_session = AsyncMock(return_value=_session_post_returning(
            _mock_json_response(200, _fs_envelope(
                "<html><title>Just a moment...</title></html>"))
        ))
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://letterboxd.com/u/list/slug/")

    async def test_flaresolverr_unreachable_raises(self):
        client = _lb_client()
        session = MagicMock()
        session.post = MagicMock(side_effect=aiohttp.ClientError("refused"))
        client._get_session = AsyncMock(return_value=session)
        with self.assertRaises(ListFetchError):
            await client.fetch_list("https://letterboxd.com/u/list/slug/")


# ---------------------------------------------------------------------------
# ListSyncService
# ---------------------------------------------------------------------------

def _seer(enqueue_result=True):
    seer = MagicMock()
    seer.request_media = AsyncMock(return_value=enqueue_result)
    return seer


def _trakt_stub(items):
    stub = MagicMock()
    stub.fetch_list = AsyncMock(return_value=items)
    return stub


class TestListSync(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_requests_all(self):
        seer = _seer(True)
        svc = ListSyncService(seer, trakt_client=_trakt_stub(
            [("603", "movie"), ("1396", "tv"), ("278", "movie")]
        ))
        summary = await svc.sync_list({"source": "trakt", "url": "u", "name": "L"})
        self.assertEqual(summary["fetched"], 3)
        self.assertEqual(summary["requested"], 3)
        self.assertEqual(len(summary["media_ids"]), 3)
        self.assertEqual(seer.request_media.await_count, 3)

    async def test_passes_source_none_and_rationale_with_name(self):
        seer = _seer(True)
        svc = ListSyncService(seer, trakt_client=_trakt_stub([("603", "movie")]))
        await svc.sync_list({"source": "trakt", "url": "http://x", "name": "Faves"})
        args, kwargs = seer.request_media.call_args
        self.assertEqual(args[0], "movie")
        self.assertEqual(args[1], {"id": "603"})
        self.assertIsNone(kwargs["source"])
        self.assertIn("Faves", kwargs["rationale"])

    async def test_dedup_skips_already_requested(self):
        seer = _seer(True)
        svc = ListSyncService(seer, trakt_client=_trakt_stub(
            [("603", "movie"), ("1396", "tv")]
        ))
        summary = await svc.sync_list(
            {"source": "trakt", "url": "u", "name": "L"},
            already_requested=[("movie", "603")],
        )
        self.assertEqual(summary["skipped_dedup"], 1)
        self.assertEqual(summary["requested"], 1)
        self.assertEqual(seer.request_media.await_count, 1)

    async def test_media_type_filter(self):
        seer = _seer(True)
        svc = ListSyncService(seer, trakt_client=_trakt_stub(
            [("603", "movie"), ("1396", "tv")]
        ))
        summary = await svc.sync_list(
            {"source": "trakt", "url": "u", "name": "L", "media_type": "movie"}
        )
        self.assertEqual(summary["skipped_media_type"], 1)
        self.assertEqual(summary["requested"], 1)

    async def test_max_items_cap(self):
        seer = _seer(True)
        svc = ListSyncService(
            seer, trakt_client=_trakt_stub([(str(i), "movie") for i in range(5)]),
            max_items=2,
        )
        summary = await svc.sync_list({"source": "trakt", "url": "u", "name": "L"})
        self.assertTrue(summary["capped"])
        self.assertEqual(summary["requested"], 2)
        self.assertEqual(len(summary["media_ids"]), 2)

    async def test_seer_returns_false_counts_membership_not_request(self):
        seer = _seer(False)  # duplicate at seer layer
        svc = ListSyncService(seer, trakt_client=_trakt_stub([("603", "movie")]))
        summary = await svc.sync_list({"source": "trakt", "url": "u", "name": "L"})
        self.assertEqual(summary["requested"], 0)
        self.assertEqual(len(summary["media_ids"]), 1)  # still part of collection

    async def test_per_item_error_isolated(self):
        seer = MagicMock()
        seer.request_media = AsyncMock(side_effect=[RuntimeError("x"), True])
        svc = ListSyncService(seer, trakt_client=_trakt_stub(
            [("1", "movie"), ("2", "movie")]
        ))
        summary = await svc.sync_list({"source": "trakt", "url": "u", "name": "L"})
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["requested"], 1)

    async def test_unsupported_source_raises(self):
        svc = ListSyncService(_seer())
        with self.assertRaises(ListFetchError):
            await svc.sync_list({"source": "imdb", "url": "u", "name": "L"})

    async def test_unconfigured_source_client_raises(self):
        svc = ListSyncService(_seer(), trakt_client=None)
        with self.assertRaises(ListFetchError):
            await svc.sync_list({"source": "trakt", "url": "u", "name": "L"})

    async def test_letterboxd_source_dispatch(self):
        seer = _seer(True)
        lb = MagicMock()
        lb.fetch_list = AsyncMock(return_value=[("496243", "movie")])
        svc = ListSyncService(seer, letterboxd_client=lb)
        summary = await svc.sync_list({"source": "letterboxd", "url": "u", "name": "L"})
        lb.fetch_list.assert_awaited_once()
        self.assertEqual(summary["requested"], 1)


if __name__ == "__main__":
    unittest.main()
