"""API routes for managing monitored Trakt/Letterboxd lists.

Provides list/add/remove and an on-demand sync-now endpoint.  Handlers are thin:
validation lives here, all behaviour is delegated to :class:`ListService`.
"""
from typing import Any

from flask import Blueprint, jsonify, request

from api_service.auth.middleware import require_role
from api_service.config.logger_manager import LoggerManager
from api_service.exceptions.api_exceptions import ListFetchError
from api_service.services.lists.list_service import ListService
from api_service.utils.asyncio_loop import run_coroutine_sync

logger = LoggerManager.get_logger("ListsRoute")
lists_bp = Blueprint("lists", __name__)
lists_bp.strict_slashes = False

_VALID_SOURCES = {"trakt", "letterboxd"}
_VALID_MEDIA_TYPES = {"movie", "tv"}


def _validate_create(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate an add-list request body.

    :return: ``(cleaned_data, None)`` on success, or ``(None, error_message)``.
    """
    if not isinstance(body, dict):
        return None, "Request body must be a JSON object."

    name = (body.get("name") or "").strip()
    source = (body.get("source") or "").strip().lower()
    url = (body.get("url") or "").strip()

    if not name:
        return None, "Field 'name' is required."
    if source not in _VALID_SOURCES:
        return None, f"Field 'source' must be one of {sorted(_VALID_SOURCES)}."
    if not url:
        return None, "Field 'url' is required."

    media_type = body.get("media_type")
    if media_type not in (None, "", *_VALID_MEDIA_TYPES):
        return None, f"Field 'media_type' must be one of {sorted(_VALID_MEDIA_TYPES)} or omitted."

    interval = body.get("sync_interval_hours", 24)
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        return None, "Field 'sync_interval_hours' must be an integer."
    if interval <= 0:
        return None, "Field 'sync_interval_hours' must be positive."

    cleaned = {
        "name": name,
        "source": source,
        "url": url,
        "media_type": media_type or None,
        "quality": (body.get("quality") or None),
        "collection_enabled": bool(body.get("collection_enabled", True)),
        "sync_interval_hours": interval,
        "enabled": bool(body.get("enabled", True)),
    }
    return cleaned, None


@lists_bp.route("", methods=["GET"])
def get_lists():
    """Return all monitored lists."""
    try:
        return jsonify({"status": "success", "lists": ListService().list_all()}), 200
    except Exception as exc:  # noqa: BLE001
        logger.error("Error listing monitored lists: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred"}), 500


@lists_bp.route("", methods=["POST"])
@require_role("admin")
def add_list():
    """Add a monitored list (admin only)."""
    cleaned, error = _validate_create(request.get_json(silent=True) or {})
    if error:
        return jsonify({"status": "error", "message": error}), 400
    try:
        created = ListService().create(cleaned)
        return jsonify({"status": "success", "list": created}), 201
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.error("Error creating monitored list: %s", exc, exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred"}), 500


@lists_bp.route("/<int:list_id>", methods=["DELETE"])
@require_role("admin")
def remove_list(list_id: int):
    """Remove a monitored list, its dedup items, and its Plex collection (admin)."""
    try:
        if run_coroutine_sync(ListService().remove(list_id), logger):
            return jsonify({"status": "success", "message": "List removed"}), 200
        return jsonify({"status": "error", "message": "List not found"}), 404
    except Exception as exc:  # noqa: BLE001
        logger.error("Error removing monitored list %s: %s", list_id, exc, exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred"}), 500


@lists_bp.route("/<int:list_id>/sync", methods=["POST"])
@require_role("admin")
def sync_list_now(list_id: int):
    """Synchronise a single monitored list immediately (admin only)."""
    try:
        summary = run_coroutine_sync(ListService().sync_list(list_id), logger)
        return jsonify({"status": "success", "summary": summary}), 200
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 404
    except ListFetchError as exc:
        # Upstream (Trakt/Letterboxd/FlareSolverr) could not be fetched.
        return jsonify({"status": "error", "message": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001
        logger.error("Error syncing monitored list %s: %s", list_id, exc, exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred"}), 500
