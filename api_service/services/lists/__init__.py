"""Monitored-list ingestion services.

Fetch curated third-party lists (Trakt, Letterboxd) and translate them into
``(tmdb_id, media_type)`` pairs that can be requested through Jellyseerr.
"""

from api_service.services.lists.trakt import TraktListClient
from api_service.services.lists.letterboxd import LetterboxdListClient
from api_service.services.lists.list_sync import ListSyncService
from api_service.services.lists.list_service import ListService

__all__ = [
    "TraktListClient",
    "LetterboxdListClient",
    "ListSyncService",
    "ListService",
]
