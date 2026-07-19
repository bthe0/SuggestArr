"""Client for fetching curated Trakt lists into ``(tmdb_id, media_type)`` pairs.

Trakt exposes a clean JSON API; every list item carries the media's external
ids including ``ids.tmdb``.  A Trakt ``client_id`` (API key) is required and is
supplied by the caller (from config/env) — never hard-coded here.

Supported list URL shapes:
    - ``https://trakt.tv/users/<user>/lists/<slug>``   (personal / public list)
    - ``https://trakt.tv/users/<user>/watchlist``       (a user's watchlist)
    - ``https://trakt.tv/lists/<id>``                    (standalone/official list)
"""

from urllib.parse import urlparse

import aiohttp

from api_service.services.http.base_client import BaseHTTPClient
from api_service.exceptions.api_exceptions import ListFetchError

TRAKT_API_BASE = "https://api.trakt.tv"

# Trakt item ``type`` -> SuggestArr media_type. Episodes/seasons/people are skipped.
_TYPE_MAP = {"movie": "movie", "show": "tv"}

# Trakt paginates list items; walk at most this many pages regardless of size.
_MAX_PAGES = 20
_PER_PAGE = 100


class TraktListClient(BaseHTTPClient):
    """Fetches a Trakt list and returns ``(tmdb_id, media_type)`` tuples."""

    def __init__(self, client_id: str, api_base: str = TRAKT_API_BASE):
        """Initialise the client.

        :param client_id: Trakt API key (``trakt-api-key`` header value).
        :param api_base: Base URL of the Trakt API (override for testing).
        """
        super().__init__()
        self.client_id = client_id
        self.api_base = api_base.rstrip("/")

    def _headers(self) -> dict:
        """Return the required Trakt API headers."""
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
        }

    @staticmethod
    def parse_list_url(url: str) -> str:
        """Translate a trakt.tv list URL into the API items path.

        :param url: A trakt.tv list URL (or a bare ``users/.../lists/...`` path).
        :return: API path (without leading slash) whose ``GET`` yields list items.
        :raises ListFetchError: If the URL does not match a supported shape.
        """
        if not url or not str(url).strip():
            raise ListFetchError("Empty Trakt list URL.", "Trakt")

        parsed = urlparse(str(url).strip())
        path = (parsed.path or parsed.netloc or "").strip("/")
        if not path and parsed.path is None:
            path = str(url).strip().strip("/")
        segments = [s for s in path.split("/") if s]

        # users/<user>/watchlist
        if len(segments) >= 3 and segments[0] == "users" and segments[2] == "watchlist":
            return f"users/{segments[1]}/watchlist"

        # users/<user>/lists/<slug>
        if len(segments) >= 4 and segments[0] == "users" and segments[2] == "lists":
            return f"users/{segments[1]}/lists/{segments[3]}/items"

        # lists/<id>  (standalone / official list)
        if len(segments) >= 2 and segments[0] == "lists":
            return f"lists/{segments[1]}/items"

        raise ListFetchError(
            f"Unrecognised Trakt list URL: {url!r}. Expected a users/lists, "
            "users/watchlist, or lists/<id> URL.",
            "Trakt",
        )

    async def fetch_list(self, url: str) -> list[tuple[str, str]]:
        """Fetch every resolvable item in the Trakt list.

        :param url: Trakt list URL.
        :return: Deduplicated list of ``(tmdb_id, media_type)`` tuples in list order.
        :raises ListFetchError: On missing key, auth failure, 404, or network error.
        """
        if not self.client_id:
            raise ListFetchError(
                "Trakt client_id is not configured. Set TRAKT_CLIENT_ID.", "Trakt"
            )

        path = self.parse_list_url(url)
        results: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for page in range(1, _MAX_PAGES + 1):
            items = await self._fetch_page(path, page)
            if not items:
                break
            for entry in items:
                pair = self._extract_pair(entry)
                if pair and pair not in seen:
                    seen.add(pair)
                    results.append(pair)
            if len(items) < _PER_PAGE:
                break  # last page reached

        self.logger.info("Trakt list %s resolved %d item(s).", path, len(results))
        return results

    async def _fetch_page(self, path: str, page: int) -> list[dict]:
        """Fetch a single page of list items; return the raw JSON array."""
        url = f"{self.api_base}/{path}?page={page}&limit={_PER_PAGE}"
        session = await self._get_session()
        try:
            async with session.get(
                url, headers=self._headers(), timeout=self.REQUEST_TIMEOUT
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data if isinstance(data, list) else []
                if response.status in (401, 403):
                    raise ListFetchError(
                        f"Trakt rejected the API key (HTTP {response.status}). "
                        "Check TRAKT_CLIENT_ID.",
                        "Trakt",
                    )
                if response.status == 404:
                    raise ListFetchError(f"Trakt list not found (404): {path}", "Trakt")
                raise ListFetchError(
                    f"Trakt returned HTTP {response.status} for {path}.", "Trakt"
                )
        except aiohttp.ClientError as exc:
            raise ListFetchError(
                f"Network error fetching Trakt list: {exc}", "Trakt"
            ) from exc

    @staticmethod
    def _extract_pair(entry: dict) -> tuple[str, str] | None:
        """Map a Trakt list entry to ``(tmdb_id, media_type)`` or ``None`` to skip."""
        if not isinstance(entry, dict):
            return None
        trakt_type = entry.get("type")
        media_type = _TYPE_MAP.get(trakt_type)
        if not media_type:
            return None  # episode / season / person — not requestable here
        media_obj = entry.get(trakt_type) or {}
        tmdb_id = (media_obj.get("ids") or {}).get("tmdb")
        if not tmdb_id:
            return None
        return (str(tmdb_id), media_type)
