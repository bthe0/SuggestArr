"""FastMCP server exposing SuggestArr monitored-list management.

Tools:
  - ``add_list(url, ...)``   — add a Trakt/Letterboxd list (source auto-detected)
  - ``list_lists()``          — list all monitored lists
  - ``remove_list(list_id)``  — remove a list (and its Plex collection)
  - ``sync_list(list_id)``    — sync a list now (fetch → request → collection)

Runs over STDIO (``mcp.run()``).  It shares SuggestArr's database and config,
so lists added here appear in the SuggestArr UI and use its Trakt/TMDB/
FlareSolverr/Plex settings — provided it runs with the same ``/app/config``
(bind-mounted ``requests.db`` + ``config.yaml``) as the SuggestArr service.
"""

import logging
import sys
from typing import Any
from urllib.parse import urlparse


def _redirect_app_logging_to_stderr() -> None:
    """Keep stdout clean for the MCP JSON-RPC transport.

    SuggestArr's ``LoggerManager`` attaches a ``StreamHandler(sys.stdout)`` to
    every logger.  On an MCP stdio server, any stdout write corrupts the
    JSON-RPC stream, so we wrap ``get_logger`` (in this process only) to move any
    stdout handler to stderr.  The file handler (app.log) is left untouched.
    """
    from api_service.config import logger_manager as _lm

    original = _lm.LoggerManager.get_logger

    def _stderr_get_logger(*args: Any, **kwargs: Any):
        logger = original(*args, **kwargs)
        for handler in logger.handlers:
            if (
                isinstance(handler, logging.StreamHandler)
                and getattr(handler, "stream", None) is sys.stdout
            ):
                handler.setStream(sys.stderr)
        return logger

    _lm.LoggerManager.get_logger = staticmethod(_stderr_get_logger)


# Must run before importing app modules that create module-level loggers.
_redirect_app_logging_to_stderr()

from fastmcp import FastMCP  # noqa: E402

from api_service.services.lists.list_service import ListService  # noqa: E402

mcp = FastMCP("suggestarr-lists")

_SOURCE_HOSTS = {"trakt": "trakt.tv", "letterboxd": "letterboxd.com"}


def _detect_source(url: str) -> str | None:
    """Infer 'trakt'/'letterboxd' from a list URL, or ``None`` if unknown."""
    lowered = (url or "").lower()
    for source, host in _SOURCE_HOSTS.items():
        if host in lowered:
            return source
    return None


def _derive_name(url: str) -> str:
    """Derive a human-readable list name from the URL's slug."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    slug = None
    if segments:
        slug = segments[-2] if segments[-1] in ("rss",) and len(segments) >= 2 else segments[-1]
    pretty = (slug or url).replace("-", " ").replace("_", " ").strip().title()
    return pretty or url


@mcp.tool
def add_list(
    url: str,
    source: str | None = None,
    name: str | None = None,
    quality: str | None = None,
    collection: bool = True,
    media_type: str | None = None,
    sync_interval_hours: int = 24,
) -> dict[str, Any]:
    """Add a monitored Trakt or Letterboxd list to SuggestArr.

    ``source`` is auto-detected from the URL when omitted (trakt.tv /
    letterboxd.com).  ``collection`` toggles the per-list Plex collection.
    ``media_type`` optionally restricts to 'movie' or 'tv'.  Returns the stored
    monitored-list row.
    """
    detected = (source or _detect_source(url) or "").lower()
    if detected not in ("trakt", "letterboxd"):
        raise ValueError(
            "Could not determine the list source from the URL; "
            "pass source='trakt' or source='letterboxd'."
        )
    if media_type not in (None, "movie", "tv"):
        raise ValueError("media_type must be 'movie', 'tv', or omitted.")

    data = {
        "name": name or _derive_name(url),
        "source": detected,
        "url": url,
        "quality": quality,
        "collection_enabled": bool(collection),
        "media_type": media_type,
        "sync_interval_hours": int(sync_interval_hours),
    }
    try:
        return ListService().create(data)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface DB errors (e.g. duplicate url)
        raise ValueError(
            f"Could not add list (is this URL already monitored?): {exc}"
        ) from exc


@mcp.tool
def list_lists() -> list[dict[str, Any]]:
    """List all monitored Trakt/Letterboxd lists with their status."""
    return ListService().list_all()


@mcp.tool
async def remove_list(list_id: int) -> dict[str, Any]:
    """Remove a monitored list by id (also deletes its Plex collection)."""
    removed = await ListService().remove(list_id)
    return {"removed": removed, "id": list_id}


@mcp.tool
async def sync_list(list_id: int) -> dict[str, Any]:
    """Sync a monitored list now: fetch items, request them, update the collection."""
    return await ListService().sync_list(list_id)


if __name__ == "__main__":
    mcp.run()
