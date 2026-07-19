"""Client for fetching public Letterboxd lists into ``(tmdb_id, 'movie')`` pairs.

Letterboxd has no official API, no per-list RSS feed (Letterboxd removed list
RSS — ``.../rss/`` now 404s), and is fronted by Cloudflare bot protection.  We
therefore route the list **page** through **FlareSolverr** (a headless-browser
challenge solver already running in the stack): we POST the list URL to
FlareSolverr's ``/v1`` endpoint, take the solved HTML from ``solution.response``,
extract each film's ``"Title (YYYY)"`` from the ``LazyPoster`` react-components
(``data-item-name`` attribute), and match title+year to a TMDB id via the
existing :class:`TMDbClient` (the list markup carries no TMDB id).

If ``FLARESOLVERR_URL`` is not configured, or FlareSolverr cannot clear the
challenge, or the list page cannot be parsed, a :class:`ListFetchError` is raised
loudly rather than returning an empty list.  Trakt has no such requirement.

.. note::
   Only the first list page (up to 100 films) is fetched — more than the 50-item
   per-sync guardrail applied downstream, so pagination is unnecessary here.
"""

import html as _html
import re

import aiohttp

from api_service.services.http.base_client import BaseHTTPClient
from api_service.exceptions.api_exceptions import ListFetchError

# Each film on a list page is a LazyPoster react-component carrying
# data-item-name="Title (YYYY)".
_ITEM_NAME_RE = re.compile(r'data-item-name="([^"]*)"')
# "Title (1999)" -> title + trailing 4-digit year (in parentheses).
_TITLE_YEAR_RE = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)\s*$")

# Markers indicating an unsolved Cloudflare interstitial or a missing list.
_CHALLENGE_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform")
_NOTFOUND_MARKERS = ("letterboxd - not found", "letterboxd &#8226; not found")

DEFAULT_MAX_TIMEOUT_MS = 40000


class LetterboxdListClient(BaseHTTPClient):
    """Fetches a Letterboxd list (via FlareSolverr) as ``(tmdb_id, 'movie')`` tuples."""

    def __init__(self, tmdb_client, flaresolverr_url: str | None = None,
                 max_timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS):
        """Initialise the client.

        :param tmdb_client: A :class:`TMDbClient` used to match title/year to a
            TMDB id (list markup carries no TMDB id).
        :param flaresolverr_url: FlareSolverr ``/v1`` endpoint (e.g.
            ``http://flaresolverr:8191/v1``).  Required for live fetches.
        :param max_timeout_ms: ``maxTimeout`` passed to FlareSolverr (ms).
        """
        super().__init__()
        self.tmdb_client = tmdb_client
        self.flaresolverr_url = (flaresolverr_url or "").strip() or None
        self.max_timeout_ms = max_timeout_ms

    @staticmethod
    def list_page_url(url: str) -> str:
        """Return the canonical list-page URL (strips query/fragment/legacy rss)."""
        cleaned = str(url).strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if cleaned.endswith("/rss"):
            cleaned = cleaned[:-4].rstrip("/")
        return cleaned + "/"

    async def fetch_list(self, url: str) -> list[tuple[str, str]]:
        """Fetch and resolve every film in the Letterboxd list.

        :param url: A Letterboxd list URL.
        :return: Deduplicated list of ``(tmdb_id, 'movie')`` tuples in list order.
        :raises ListFetchError: If FlareSolverr is unconfigured/unreachable, the
            challenge persists, the list is missing, or no films can be parsed.
        """
        if not self.flaresolverr_url:
            raise ListFetchError(
                "Letterboxd requires FlareSolverr to clear Cloudflare, but "
                "FLARESOLVERR_URL is not configured.",
                "Letterboxd",
            )
        html_text = await self._fetch_via_flaresolverr(url)
        names = self._parse_item_names(html_text)
        if not names:
            raise ListFetchError(
                "No films parsed from the Letterboxd list page — the list may be "
                "empty/private, or Letterboxd's markup changed.",
                "Letterboxd",
            )
        return await self._resolve(names)

    async def _fetch_via_flaresolverr(self, url: str) -> str:
        """POST the list-page URL to FlareSolverr and return the solved HTML."""
        page_url = self.list_page_url(url)
        payload = {
            "cmd": "request.get",
            "url": page_url,
            "maxTimeout": self.max_timeout_ms,
        }
        # FlareSolverr may take up to maxTimeout to solve; allow headroom over the
        # base client's short REQUEST_TIMEOUT.
        timeout = aiohttp.ClientTimeout(total=self.max_timeout_ms / 1000 + 15)
        session = await self._get_session()
        try:
            async with session.post(
                self.flaresolverr_url, json=payload, timeout=timeout
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:200]
                    raise ListFetchError(
                        f"FlareSolverr returned HTTP {response.status}: {body}",
                        "Letterboxd",
                    )
                data = await response.json()
        except aiohttp.ClientError as exc:
            raise ListFetchError(
                f"Cannot reach FlareSolverr at {self.flaresolverr_url}: {exc}",
                "Letterboxd",
            ) from exc

        if str(data.get("status", "")).lower() != "ok":
            raise ListFetchError(
                f"FlareSolverr did not solve the Letterboxd challenge: "
                f"{data.get('message')!r}",
                "Letterboxd",
            )

        solution = data.get("solution") or {}
        http_status = solution.get("status")
        html_text = solution.get("response") or ""
        if http_status is not None and int(http_status) != 200:
            raise ListFetchError(
                f"Letterboxd returned HTTP {http_status} via FlareSolverr "
                f"for {page_url}.",
                "Letterboxd",
            )

        head = html_text[:4000].lower()
        if any(marker in head for marker in _CHALLENGE_MARKERS):
            raise ListFetchError(
                "Letterboxd Cloudflare challenge persisted through FlareSolverr.",
                "Letterboxd",
            )
        if any(marker in head for marker in _NOTFOUND_MARKERS):
            raise ListFetchError(f"Letterboxd list not found: {page_url}", "Letterboxd")
        return html_text

    @classmethod
    def _parse_item_names(cls, html_text: str) -> list[tuple[str, str | None]]:
        """Extract ordered, deduplicated ``(title, year)`` pairs from list HTML."""
        seen: set[tuple[str, str | None]] = set()
        out: list[tuple[str, str | None]] = []
        for raw in _ITEM_NAME_RE.findall(html_text):
            name = _html.unescape(raw).strip()
            if not name:
                continue
            match = _TITLE_YEAR_RE.match(name)
            if match:
                title, year = match.group("title").strip(), match.group("year")
            else:
                title, year = name, None
            key = (title, year)
            if title and key not in seen:
                seen.add(key)
                out.append((title, year))
        return out

    async def _resolve(self, names: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        """Resolve ``(title, year)`` pairs to deduplicated ``(tmdb_id, 'movie')``."""
        results: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for title, year in names:
            tmdb_id = await self._search_tmdb(title, year)
            if not tmdb_id:
                self.logger.info("Letterboxd: no TMDB match for '%s' (%s).", title, year)
                continue
            pair = (str(tmdb_id), "movie")
            if pair not in seen:
                seen.add(pair)
                results.append(pair)
        self.logger.info(
            "Letterboxd list resolved %d/%d film(s).", len(results), len(names)
        )
        return results

    async def _search_tmdb(self, title: str, year: str | None):
        """Return the first TMDB movie id matching ``title`` (+ optional ``year``)."""
        results = await self.tmdb_client.search_movie(title, year)
        if results:
            return results[0].get("id")
        # Retry without the year — Letterboxd release years occasionally differ
        # from TMDB's primary release year.
        if year:
            results = await self.tmdb_client.search_movie(title, None)
            if results:
                return results[0].get("id")
        return None
