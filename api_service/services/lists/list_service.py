"""Orchestration service for monitored lists.

Ties the persistence layer (:class:`ListRepository`) to the sync engine
(:class:`ListSyncService`), building the Trakt/Letterboxd/Seer/TMDB clients from
runtime config.  The REST blueprint calls this service so route handlers stay
thin.
"""
import json
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any

from api_service.config.config import load_env_vars
from api_service.config.logger_manager import LoggerManager
from api_service.db.list_repository import ListRepository
from api_service.exceptions.api_exceptions import ListFetchError
from api_service.services.seer.seer_client import SeerClient
from api_service.services.tmdb.tmdb_client import TMDbClient
from api_service.services.plex.library_service import PlexLibraryService
from api_service.services.lists.trakt import TraktListClient
from api_service.services.lists.letterboxd import LetterboxdListClient
from api_service.services.lists.list_sync import ListSyncService, DEFAULT_MAX_ITEMS


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class ListService:
    """CRUD + on-demand sync for monitored Trakt/Letterboxd lists."""

    def __init__(self, repository: ListRepository | None = None) -> None:
        """Initialise with an optional repository (injected for testing)."""
        self.repo = repository or ListRepository()
        self.logger = LoggerManager.get_logger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Thin CRUD pass-throughs (route handlers stay thin)
    # ------------------------------------------------------------------
    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a monitored list and return the stored row."""
        list_id = self.repo.create_list(data)
        return self.repo.get_list(list_id)

    def list_all(self) -> list[dict[str, Any]]:
        """Return all monitored lists."""
        return self.repo.get_all_lists()

    def get(self, list_id: int) -> dict[str, Any] | None:
        """Return one monitored list, or ``None``."""
        return self.repo.get_list(list_id)

    def delete(self, list_id: int) -> bool:
        """Delete a monitored list row (items cascade). Return True if removed.

        Does not touch Plex — use :meth:`remove` to also delete the collection.
        """
        return self.repo.delete_list(list_id)

    async def remove(self, list_id: int) -> bool:
        """Delete a monitored list and its Plex collection (best-effort).

        The DB row is always removed if present; a Plex collection-delete failure
        is logged but does not block removal.
        """
        row = self.repo.get_list(list_id)
        if not row:
            return False
        collection_id = row.get("collection_id")
        if collection_id:
            plex = self._build_plex(load_env_vars())
            if plex is not None:
                try:
                    async with plex:
                        await plex.delete_collection(collection_id)
                    self.logger.info(
                        "Deleted Plex collection %s for list %s.", collection_id, list_id
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                    self.logger.error(
                        "Failed to delete Plex collection %s for list %s: %s",
                        collection_id, list_id, exc,
                    )
        return self.repo.delete_list(list_id)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    async def sync_list(self, list_id: int) -> dict[str, Any]:
        """Fetch and request the items of one monitored list.

        Builds the source client(s) from config, runs the sync, persists the
        resulting membership + dedup state, and stamps the list's last-sync
        status.  Returns the sync summary.

        :raises ValueError: If the list id is unknown.
        :raises ListFetchError: If the source cannot be fetched.
        """
        row = self.repo.get_list(list_id)
        if not row:
            raise ValueError(f"Monitored list {list_id} not found.")

        env = load_env_vars()
        source = (row.get("source") or "").lower()
        already = self.repo.get_requested_keys(list_id)

        try:
            async with AsyncExitStack() as stack:
                seer = await stack.enter_async_context(self._build_seer(env))
                trakt = None
                letterboxd = None
                if source == "trakt":
                    trakt = await stack.enter_async_context(
                        TraktListClient(env.get("TRAKT_CLIENT_ID") or "")
                    )
                elif source == "letterboxd":
                    tmdb = await stack.enter_async_context(self._build_tmdb(env))
                    letterboxd = await stack.enter_async_context(
                        LetterboxdListClient(
                            tmdb, flaresolverr_url=env.get("FLARESOLVERR_URL") or ""
                        )
                    )
                sync = ListSyncService(
                    seer,
                    trakt_client=trakt,
                    letterboxd_client=letterboxd,
                    max_items=DEFAULT_MAX_ITEMS,
                )
                summary = await sync.sync_list(row, already)

                # Build/update the Plex collection from the in-cap membership.
                summary["collection"] = await self._build_collection(row, summary, env)
        except ListFetchError as exc:
            self.repo.update_list(
                list_id,
                {
                    "last_synced_at": _now_iso(),
                    "last_status": "error",
                    "last_summary": json.dumps({"error": str(exc)}),
                },
            )
            self.logger.error("Sync failed for list %s: %s", list_id, exc)
            raise

        self.repo.record_sync_items(
            list_id, summary["media_ids"], summary["requested_ids"]
        )
        updates: dict[str, Any] = {
            "last_synced_at": _now_iso(),
            "last_status": "success",
            "last_summary": json.dumps(summary),
        }
        collection = summary.get("collection") or {}
        if collection.get("collection_id"):
            updates["collection_id"] = collection["collection_id"]
        self.repo.update_list(list_id, updates)
        return summary

    async def _build_collection(
        self, row: dict[str, Any], summary: dict[str, Any], env: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Create/update the Plex collection for a synced list (best-effort).

        Returns a collection summary, ``None`` when collections are disabled/not
        applicable, or ``{'error': ...}`` on failure (never raises).
        """
        if not row.get("collection_enabled"):
            return None
        plex = self._build_plex(env)
        if plex is None:
            return None
        # Plex collections are single-type + section-scoped; collect movies only.
        movie_ids = [
            m["tmdb_id"] for m in summary["media_ids"] if m["media_type"] == "movie"
        ]
        if not movie_ids:
            self.logger.info("List '%s': no movie items for a Plex collection.", row.get("name"))
            return None
        try:
            async with plex:
                section_id = await self._movie_section_id(plex, env)
                if not section_id:
                    self.logger.warning("No Plex movie library found; skipping collection.")
                    return {"error": "no movie library section found"}
                return await plex.sync_collection(
                    section_id, row["name"], movie_ids, "movie"
                )
        except Exception as exc:  # noqa: BLE001 — collection build must not fail the sync
            self.logger.error("Collection build failed for '%s': %s", row.get("name"), exc)
            return {"error": str(exc)}

    @staticmethod
    async def _movie_section_id(plex: PlexLibraryService, env: dict[str, Any]) -> str | None:
        """Pick the Plex movie library section id (honouring PLEX_LIBRARIES)."""
        movie_libs = await plex.filter_libraries_by_type("movie")
        if not movie_libs:
            return None
        configured = env.get("PLEX_LIBRARIES") or []
        configured_ids = {
            str(lib.get("id")) for lib in configured
            if isinstance(lib, dict) and lib.get("id") is not None
        }
        if configured_ids:
            for lib in movie_libs:
                if str(lib.get("key")) in configured_ids:
                    return str(lib.get("key"))
        return str(movie_libs[0].get("key"))

    @staticmethod
    def _build_plex(env: dict[str, Any]) -> PlexLibraryService | None:
        """Build a PlexLibraryService when Plex is the selected, configured service."""
        if str(env.get("SELECTED_SERVICE") or "").lower() != "plex":
            return None
        api_url = env.get("PLEX_API_URL")
        token = env.get("PLEX_TOKEN")
        if not api_url or not token:
            return None
        return PlexLibraryService(token=token, api_url=api_url)

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------
    @staticmethod
    def _build_seer(env: dict[str, Any]) -> SeerClient:
        """Build a SeerClient for enqueueing requests (no login/init needed).

        ``request_media`` only writes to the local pending-requests queue; the
        background queue worker performs the actual Jellyseerr submission.
        """
        return SeerClient(
            env.get("SEER_API_URL"),
            env.get("SEER_TOKEN"),
            env.get("SEER_USER_NAME"),
            env.get("SEER_USER_PSW"),
            env.get("SEER_SESSION_TOKEN"),
            env.get("FILTER_NUM_SEASONS") or "all",
            env.get("EXCLUDE_DOWNLOADED", True),
            env.get("EXCLUDE_REQUESTED", True),
            env.get("SEER_ANIME_PROFILE_CONFIG") or {},
            env.get("REQUEST_FIRST_SEASON_ONLY", False),
        )

    @staticmethod
    def _build_tmdb(env: dict[str, Any]) -> TMDbClient:
        """Build a minimal TMDbClient used only for Letterboxd title→id search."""
        return TMDbClient(
            env.get("TMDB_API_KEY"),
            search_size=20,
            tmdb_threshold=0,
            tmdb_min_votes=0,
            include_no_ratings=True,
            filter_release_year=0,
            filter_language=None,
            filter_genre=None,
            filter_region_provider=None,
            filter_streaming_services=None,
        )
