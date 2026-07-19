"""Repository for monitored Trakt/Letterboxd lists and their per-list dedup items.

Provides CRUD for the ``monitored_lists`` table and membership/dedup tracking in
``monitored_list_items``.  Follows the same raw-parameterised-SQL, cross-engine
(``sqlite``/``mysql``/``postgres``) pattern as :class:`JobRepository`.
"""
import json
from typing import Any

from api_service.config.logger_manager import LoggerManager
from api_service.db.database_manager import DatabaseManager

# Columns selected for a monitored list, in a fixed order (tuple-row fallback).
_LIST_COLUMNS = (
    "id", "name", "source", "url", "media_type", "quality",
    "collection_enabled", "sync_interval_hours", "enabled", "collection_id",
    "last_synced_at", "last_status", "last_summary", "created_at", "updated_at",
)

# Fields callers may update via update_list (whitelist — never interpolate keys).
_UPDATABLE = frozenset({
    "name", "media_type", "quality", "collection_enabled", "sync_interval_hours",
    "enabled", "collection_id", "last_synced_at", "last_status", "last_summary",
})

_VALID_SOURCES = ("trakt", "letterboxd")


class ListRepository:
    """Persistence for monitored lists and their requested-item dedup set."""

    def __init__(self) -> None:
        """Initialise the repository with a shared DatabaseManager."""
        self.logger = LoggerManager.get_logger(self.__class__.__name__)
        self.db = DatabaseManager()

    # ------------------------------------------------------------------
    # monitored_lists CRUD
    # ------------------------------------------------------------------
    def create_list(self, data: dict[str, Any]) -> int:
        """Insert a monitored list and return its new id.

        :param data: Must contain ``name``, ``source`` ('trakt'/'letterboxd'),
            and ``url``.  Optional: ``media_type``, ``quality``,
            ``collection_enabled`` (bool), ``sync_interval_hours`` (int),
            ``enabled`` (bool).
        :raises ValueError: If a required field is missing or ``source`` invalid.
        """
        name = (data.get("name") or "").strip()
        source = (data.get("source") or "").strip().lower()
        url = (data.get("url") or "").strip()
        if not name or not source or not url:
            raise ValueError("Monitored list requires non-empty name, source, and url.")
        if source not in _VALID_SOURCES:
            raise ValueError(f"Invalid source {source!r}; expected one of {_VALID_SOURCES}.")

        query = """
            INSERT INTO monitored_lists
                (name, source, url, media_type, quality, collection_enabled,
                 sync_interval_hours, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            name, source, url,
            data.get("media_type"),
            data.get("quality"),
            1 if data.get("collection_enabled", True) else 0,
            int(data.get("sync_interval_hours", 24) or 24),
            1 if data.get("enabled", True) else 0,
        )

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if self.db.db_type == "mysql":
                query = query.replace("?", "%s")
            elif self.db.db_type == "postgres":
                query = query.replace("?", "%s") + " RETURNING id"
            cursor.execute(query, params)
            conn.commit()
            if self.db.db_type == "postgres":
                new_id = cursor.fetchone()[0]
            else:
                new_id = cursor.lastrowid
            self.logger.info("Created monitored list id=%s (%s %s)", new_id, source, url)
            return int(new_id)

    def get_all_lists(self) -> list[dict[str, Any]]:
        """Return every monitored list, newest first."""
        return self._select_lists("ORDER BY created_at DESC")

    def get_enabled_lists(self) -> list[dict[str, Any]]:
        """Return only enabled monitored lists, newest first."""
        return self._select_lists("WHERE enabled = 1 ORDER BY created_at DESC")

    def get_list(self, list_id: int) -> dict[str, Any] | None:
        """Return a single monitored list by id, or ``None`` if absent."""
        rows = self._select_lists("WHERE id = ?", (list_id,))
        return rows[0] if rows else None

    def _select_lists(self, where_order: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Run a SELECT over monitored_lists and map rows to dicts."""
        query = f"SELECT {', '.join(_LIST_COLUMNS)} FROM monitored_lists {where_order}"
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if params and self.db.db_type in ("mysql", "postgres"):
                query = query.replace("?", "%s")
            cursor.execute(query, params)
            return [self._row_to_list(row) for row in cursor.fetchall()]

    def update_list(self, list_id: int, data: dict[str, Any]) -> bool:
        """Update whitelisted fields of a monitored list.

        :return: True if a row was updated, False if the id was unknown or no
            recognised fields were supplied.
        """
        fields: list[str] = []
        params: list[Any] = []
        for key, value in data.items():
            if key not in _UPDATABLE:
                continue
            if key in ("collection_enabled", "enabled"):
                value = 1 if value else 0
            elif key == "sync_interval_hours" and value is not None:
                value = int(value)
            fields.append(f"{key} = ?")
            params.append(value)

        if not fields:
            self.logger.warning("update_list(%s): no updatable fields supplied.", list_id)
            return False

        fields.append("updated_at = CURRENT_TIMESTAMP")
        params.append(list_id)
        query = f"UPDATE monitored_lists SET {', '.join(fields)} WHERE id = ?"

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if self.db.db_type in ("mysql", "postgres"):
                query = query.replace("?", "%s")
            cursor.execute(query, tuple(params))
            conn.commit()
            return cursor.rowcount > 0

    def delete_list(self, list_id: int) -> bool:
        """Delete a monitored list (its items cascade). Return True if removed."""
        query = "DELETE FROM monitored_lists WHERE id = ?"
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if self.db.db_type in ("mysql", "postgres"):
                query = query.replace("?", "%s")
            cursor.execute(query, (list_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                self.logger.info("Deleted monitored list id=%s", list_id)
            return deleted

    # ------------------------------------------------------------------
    # monitored_list_items — dedup + collection membership
    # ------------------------------------------------------------------
    def get_requested_keys(self, list_id: int) -> set[tuple[str, str]]:
        """Return ``(media_type, tmdb_id)`` pairs already requested for this list."""
        query = (
            "SELECT media_type, tmdb_id FROM monitored_list_items "
            "WHERE list_id = ? AND requested = 1"
        )
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if self.db.db_type in ("mysql", "postgres"):
                query = query.replace("?", "%s")
            cursor.execute(query, (list_id,))
            return {(row[0], str(row[1])) for row in cursor.fetchall()}

    def get_member_items(self, list_id: int) -> list[dict[str, Any]]:
        """Return all recorded membership rows for a list (for Plex collections)."""
        query = (
            "SELECT tmdb_id, media_type, requested FROM monitored_list_items "
            "WHERE list_id = ? ORDER BY first_seen_at ASC"
        )
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if self.db.db_type in ("mysql", "postgres"):
                query = query.replace("?", "%s")
            cursor.execute(query, (list_id,))
            return [
                {"tmdb_id": str(r[0]), "media_type": r[1], "requested": bool(r[2])}
                for r in cursor.fetchall()
            ]

    def record_sync_items(
        self,
        list_id: int,
        members: list[dict[str, Any]],
        requested: list[dict[str, Any]],
    ) -> None:
        """Upsert membership rows and mark the newly-requested ones.

        :param list_id: The owning list id.
        :param members: All in-cap items this sync considered
            (``[{'tmdb_id', 'media_type'}, ...]``) — recorded as membership.
        :param requested: Subset newly enqueued this sync — marked ``requested=1``.
        """
        if not members and not requested:
            return

        insert = """
            INSERT OR IGNORE INTO monitored_list_items
                (list_id, tmdb_id, media_type, requested)
            VALUES (?, ?, ?, 0)
        """
        mark = """
            UPDATE monitored_list_items SET requested = 1
            WHERE list_id = ? AND tmdb_id = ? AND media_type = ?
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if self.db.db_type == "mysql":
                insert = insert.replace("INSERT OR IGNORE", "INSERT IGNORE").replace("?", "%s")
                mark = mark.replace("?", "%s")
            elif self.db.db_type == "postgres":
                insert = (insert.replace("INSERT OR IGNORE", "INSERT").rstrip()
                          + " ON CONFLICT DO NOTHING").replace("?", "%s")
                mark = mark.replace("?", "%s")

            for item in members:
                cursor.execute(insert, (list_id, str(item["tmdb_id"]), item["media_type"]))
            for item in requested:
                # Ensure the row exists even if it was not in `members`.
                cursor.execute(insert, (list_id, str(item["tmdb_id"]), item["media_type"]))
                cursor.execute(mark, (list_id, str(item["tmdb_id"]), item["media_type"]))
            conn.commit()

    # ------------------------------------------------------------------
    # Row mapping
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_list(row: Any) -> dict[str, Any]:
        """Map a monitored_lists row (tuple or Row) to a normalised dict."""
        if hasattr(row, "keys"):
            data = dict(row)
        else:
            data = {col: row[i] for i, col in enumerate(_LIST_COLUMNS)}

        data["collection_enabled"] = bool(data.get("collection_enabled", 1))
        data["enabled"] = bool(data.get("enabled", 1))
        if data.get("sync_interval_hours") is not None:
            data["sync_interval_hours"] = int(data["sync_interval_hours"])

        summary = data.get("last_summary")
        if isinstance(summary, str) and summary:
            try:
                data["last_summary"] = json.loads(summary)
            except json.JSONDecodeError:
                pass  # leave as raw string if it was not JSON
        return data
