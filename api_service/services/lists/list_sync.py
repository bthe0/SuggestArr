import os
"""Synchronise monitored Trakt/Letterboxd lists into Jellyseerr requests.

For each monitored list this service:
  1. fetches ``(tmdb_id, media_type)`` items via the source client,
  2. optionally restricts to a single media_type,
  3. caps the run at ``max_items`` (guardrail — default 50 per sync per list),
  4. skips items already requested (per-list dedup set), and
  5. enqueues the rest via :meth:`SeerClient.request_media`.

Persistence of the dedup set and Plex-collection building are handled by the
callers (REST blueprint / collection service) in later increments; this service
returns a summary describing exactly what it did.
"""

from collections.abc import Iterable

from api_service.config.logger_manager import LoggerManager
from api_service.exceptions.api_exceptions import ListFetchError

#: Per-list, per-sync ceiling. Overridable with LIST_SYNC_MAX_ITEMS.
#:
#: This is a runaway guard, NOT indexer protection — Radarr queues and
#: paces its own searches, so a lower value here does not reduce indexer
#: load, it only stretches the same work across more syncs. 50 made a
#: 250-film list take days.
DEFAULT_MAX_ITEMS = int(os.environ.get('LIST_SYNC_MAX_ITEMS', '500'))
_SUPPORTED_SOURCES = ("trakt", "letterboxd")


class ListSyncService:
    """Fetches monitored lists and requests their items through Seer."""

    def __init__(
        self,
        seer_client,
        trakt_client=None,
        letterboxd_client=None,
        max_items: int = DEFAULT_MAX_ITEMS,
        logger=None,
    ):
        """Initialise the sync service.

        :param seer_client: A :class:`SeerClient` (its ``request_media`` enqueues
            + dedups internally, returning True when newly enqueued).
        :param trakt_client: A :class:`TraktListClient`, or ``None`` if unused.
        :param letterboxd_client: A :class:`LetterboxdListClient`, or ``None``.
        :param max_items: Hard cap on items processed per list per sync.
        :param logger: Optional logger; a module logger is created otherwise.
        """
        self.seer_client = seer_client
        self.trakt_client = trakt_client
        self.letterboxd_client = letterboxd_client
        self.max_items = max_items if max_items and max_items > 0 else DEFAULT_MAX_ITEMS
        self.logger = logger or LoggerManager.get_logger(self.__class__.__name__)

    async def _fetch_items(self, source: str, url: str) -> list[tuple[str, str]]:
        """Dispatch to the correct source client for the list URL."""
        normalized = (source or "").strip().lower()
        if normalized == "trakt":
            if not self.trakt_client:
                raise ListFetchError("Trakt client is not configured.", "Trakt")
            return await self.trakt_client.fetch_list(url)
        if normalized == "letterboxd":
            if not self.letterboxd_client:
                raise ListFetchError("Letterboxd client is not configured.", "Letterboxd")
            return await self.letterboxd_client.fetch_list(url)
        raise ListFetchError(
            f"Unsupported list source: {source!r}. Expected one of {_SUPPORTED_SOURCES}.",
            "List",
        )

    async def sync_list(
        self,
        monitored_list: dict,
        already_requested: Iterable[tuple[str, str]] | None = None,
    ) -> dict:
        """Fetch one monitored list and request its (non-deduped) items.

        :param monitored_list: Dict with at least ``source`` and ``url``.  Optional
            keys: ``name`` (defaults to the URL), ``media_type`` (restrict to
            'movie'/'tv'), ``quality`` (stored/passed through by callers).
        :param already_requested: Iterable of ``(media_type, tmdb_id)`` tuples
            previously requested for this list; matching items are skipped.
        :return: Summary dict describing the sync (counts + membership lists).
        :raises ListFetchError: If the source is unsupported/unconfigured or the
            fetch fails.
        """
        name = monitored_list.get("name") or monitored_list.get("url")
        source = monitored_list.get("source")
        url = monitored_list.get("url")
        wanted_type = monitored_list.get("media_type")
        already = {tuple(k) for k in (already_requested or ())}

        summary = {
            "name": name,
            "source": source,
            "url": url,
            "fetched": 0,
            "requested": 0,
            "skipped_dedup": 0,
            "skipped_media_type": 0,
            "capped": False,
            "errors": 0,
            "requested_ids": [],   # newly enqueued this run
            "media_ids": [],       # full in-cap membership (for Plex collection)
        }

        items = await self._fetch_items(source, url)
        summary["fetched"] = len(items)

        # Optional per-list media_type restriction.
        filtered: list[tuple[str, str]] = []
        for tmdb_id, media_type in items:
            if wanted_type and media_type != wanted_type:
                summary["skipped_media_type"] += 1
                continue
            filtered.append((str(tmdb_id), media_type))

        # Guardrail: cap items processed per sync per list.
        #
        # ⚠️ Order matters, and the previous order was a permanent-truncation bug:
        # capping BEFORE consulting the dedup set meant every run took the same
        # first N items, and anything past N was unreachable on every future sync
        # rather than merely deferred. Dropping already-seen items first makes the
        # cap a genuine per-run throttle — each sync advances through the list.
        unseen = [
            (tmdb_id, media_type)
            for tmdb_id, media_type in filtered
            if (media_type, tmdb_id) not in already
        ]
        summary["skipped_dedup"] = len(filtered) - len(unseen)
        summary["capped"] = len(unseen) > self.max_items
        capped = unseen[: self.max_items]

        rationale = f"Added from {source} list '{name}' ({url})"

        # TODO(theo): map quality->Radarr profile. The per-list `quality` cap is
        # stored on the monitored_lists row but not yet passed through to
        # Seer/Radarr here (needs a quality->profileId mapping decision).
        for tmdb_id, media_type in capped:
            summary["media_ids"].append({"tmdb_id": tmdb_id, "media_type": media_type})
            key = (media_type, tmdb_id)

            if key in already:
                # already-seen items are dropped BEFORE the cap above; this is a
                # belt-and-braces guard and must not double-count the summary.
                continue

            try:
                # NOTE: SeerClient._build_seer_payload calls ``source.get('id')``,
                # so ``source`` must be a media-like dict — a bare string would
                # crash.  List attribution is carried in ``rationale`` instead,
                # and ``source`` is left as None (a list is not a "similar-to"
                # seed item).
                enqueued = await self.seer_client.request_media(
                    media_type,
                    {"id": tmdb_id},
                    source=None,
                    rationale=rationale,
                )
            except Exception as exc:  # noqa: BLE001 — isolate one bad item
                summary["errors"] += 1
                self.logger.error(
                    "List '%s': failed to request tmdb:%s (%s): %s",
                    name, tmdb_id, media_type, exc,
                )
                continue

            # Mark as handled regardless of enqueue result so a subsequent sync
            # does not re-attempt an already-submitted item.
            already.add(key)
            if enqueued:
                summary["requested"] += 1
                summary["requested_ids"].append(
                    {"tmdb_id": tmdb_id, "media_type": media_type}
                )

        self.logger.info(
            "List '%s' (%s): fetched=%d requested=%d skipped_dedup=%d capped=%s",
            name, source, summary["fetched"], summary["requested"],
            summary["skipped_dedup"], summary["capped"],
        )
        return summary
