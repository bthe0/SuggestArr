"""
Plex library management service.
"""
from typing import Any, Dict, List
from urllib.parse import quote

from api_service.services.plex.base_client import PlexBaseClient
from api_service.services.plex.plex_client import normalize_guid_provider_id

# Plex collection library type codes.
_PLEX_TYPE = {"movie": 1, "tv": 2}


class PlexLibraryService(PlexBaseClient):
    """
    Service for managing Plex libraries.
    Handles library retrieval and content management.
    """
    
    async def get_libraries(self) -> List[Dict[str, Any]]:
        """
        Retrieve all libraries from the Plex server.
        
        Returns:
            List of library dictionaries with title, type, key, etc.
            
        Raises:
            PlexConnectionError: If API request fails
        """
        url = f"{self.api_url}/library/sections"
        response_data = await self._make_request(url)

        # Plex nests the payload under MediaContainer; fall back to the flat
        # shape for robustness against differently-shaped responses.
        container = (response_data or {}).get('MediaContainer', response_data or {})
        libraries = container.get('Directory')
        if not libraries:
            return []

        self.logger.info(f"Successfully retrieved {len(libraries)} libraries from Plex")
        return libraries
    
    async def get_library_by_id(self, library_id: str) -> Dict[str, Any]:
        """
        Retrieve specific library by ID.
        
        Args:
            library_id: Library ID to retrieve
            
        Returns:
            Library dictionary
            
        Raises:
            PlexConnectionError: If API request fails
        """
        url = f"{self.api_url}/library/sections/{library_id}"
        return await self._make_request(url)
    
    async def get_library_items(self, library_id: str, start: int = 0, size: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieve items from a specific library.
        
        Args:
            library_id: Library ID
            start: Starting offset
            size: Number of items to retrieve
            
        Returns:
            List of library items
            
        Raises:
            PlexConnectionError: If API request fails
        """
        url = f"{self.api_url}/library/sections/{library_id}/all"
        params = f"X-Plex-Container-Start={start}&X-Plex-Container-Size={size}"
        full_url = f"{url}?{params}"
        
        response_data = await self._make_request(full_url)

        container = (response_data or {}).get('MediaContainer', response_data or {})
        items = container.get('Metadata')
        if not items:
            return []

        self.logger.info(f"Retrieved {len(items)} items from library {library_id}")
        return items
    
    async def filter_libraries_by_type(self, media_type: str = None) -> List[Dict[str, Any]]:
        """
        Filter libraries by media type.
        
        Args:
            media_type: Media type to filter ('movie', 'show', 'artist')
            
        Returns:
            List of filtered libraries
        """
        libraries = await self.get_libraries()
        
        if media_type is None:
            return libraries
        
        # Map media types to Plex types
        type_mapping = {
            'movie': 'movie',
            'show': 'show', 
            'series': 'show',
            'artist': 'artist',
            'music': 'artist'
        }
        
        plex_type = type_mapping.get(media_type.lower())
        if plex_type is None:
            return []

        return [lib for lib in libraries if lib.get('type') == plex_type]

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------
    async def get_machine_identifier(self) -> str | None:
        """Return the Plex server's machineIdentifier (needed for collection URIs)."""
        data = await self._make_request(f"{self.api_url}/identity")
        return (data.get("MediaContainer") or {}).get("machineIdentifier")

    async def build_tmdb_rating_key_map(
        self, section_id: str, page_size: int = 200
    ) -> dict[str, str]:
        """Map ``tmdb_id -> ratingKey`` for every item in a library section.

        Pages through ``/all?includeGuids=1`` and reads each item's external
        Guids.  Only items exposing a TMDB guid are included.
        """
        mapping: dict[str, str] = {}
        start = 0
        while True:
            url = (
                f"{self.api_url}/library/sections/{section_id}/all"
                f"?includeGuids=1&X-Plex-Container-Start={start}"
                f"&X-Plex-Container-Size={page_size}"
            )
            data = await self._make_request(url)
            container = data.get("MediaContainer") or {}
            items = container.get("Metadata") or []
            for item in items:
                rating_key = item.get("ratingKey")
                if not rating_key:
                    continue
                for guid in item.get("Guid") or []:
                    tmdb_id = normalize_guid_provider_id(guid.get("id", ""), "tmdb")
                    if tmdb_id:
                        mapping[str(tmdb_id)] = str(rating_key)
                        break
            total = int(container.get("totalSize", container.get("size", 0)) or 0)
            start += len(items)
            if not items or start >= total:
                break
        return mapping

    async def find_collection(self, section_id: str, title: str) -> dict[str, str] | None:
        """Return ``{'ratingKey', 'title'}`` for a section collection matching title."""
        url = f"{self.api_url}/library/sections/{section_id}/collections"
        data = await self._make_request(url)
        container = data.get("MediaContainer") or {}
        for coll in container.get("Metadata") or container.get("Directory") or []:
            if coll.get("title") == title and coll.get("ratingKey") is not None:
                return {"ratingKey": str(coll.get("ratingKey")), "title": title}
        return None

    @staticmethod
    def _collection_uri(machine_id: str, rating_keys: list[str]) -> str:
        """Build the ``server://…`` metadata URI Plex expects for collection ops."""
        keys = ",".join(str(k) for k in rating_keys)
        return (
            f"server://{machine_id}/com.plexapp.plugins.library"
            f"/library/metadata/{keys}"
        )

    async def create_collection(
        self, section_id: str, title: str, rating_keys: list[str],
        machine_id: str, media_type: str = "movie",
    ) -> str | None:
        """Create a collection from ratingKeys; return its ratingKey or ``None``."""
        if not rating_keys:
            return None
        plex_type = _PLEX_TYPE.get(media_type, 1)
        uri = quote(self._collection_uri(machine_id, rating_keys), safe="")
        url = (
            f"{self.api_url}/library/collections?type={plex_type}"
            f"&title={quote(str(title))}&smart=0&sectionId={section_id}&uri={uri}"
        )
        data = await self._make_request(url, method="POST")
        container = data.get("MediaContainer") or {}
        created = container.get("Metadata") or container.get("Directory") or []
        rating_key = created[0].get("ratingKey") if created else None
        return str(rating_key) if rating_key is not None else None

    async def add_to_collection(
        self, collection_id: str, rating_keys: list[str], machine_id: str
    ) -> bool:
        """Add ratingKeys to an existing collection (idempotent on Plex)."""
        if not rating_keys:
            return False
        uri = quote(self._collection_uri(machine_id, rating_keys), safe="")
        url = f"{self.api_url}/library/collections/{collection_id}/items?uri={uri}"
        await self._make_request(url, method="PUT")
        return True

    async def delete_collection(self, collection_id: str) -> bool:
        """Delete a collection by its ratingKey."""
        url = f"{self.api_url}/library/collections/{collection_id}"
        await self._make_request(url, method="DELETE")
        return True

    async def sync_collection(
        self, section_id: str, title: str, tmdb_ids: list[str],
        media_type: str = "movie",
    ) -> dict[str, Any]:
        """Ensure a collection named ``title`` contains the given TMDB ids.

        Resolves each TMDB id to a Plex ratingKey in the section, then creates the
        collection (if absent) or adds the resolved items to the existing one
        (additive union — items dropped from the list are not removed here).

        :return: Summary dict with ``collection_id``, ``matched``, ``missing``,
            and ``created``.
        """
        result: dict[str, Any] = {
            "title": title, "collection_id": None,
            "matched": 0, "missing": [], "created": False,
        }
        mapping = await self.build_tmdb_rating_key_map(section_id)
        rating_keys: list[str] = []
        for tmdb_id in tmdb_ids:
            rating_key = mapping.get(str(tmdb_id))
            if rating_key:
                rating_keys.append(rating_key)
            else:
                result["missing"].append(str(tmdb_id))
        result["matched"] = len(rating_keys)
        if not rating_keys:
            return result

        machine_id = await self.get_machine_identifier()
        existing = await self.find_collection(section_id, title)
        if existing:
            await self.add_to_collection(existing["ratingKey"], rating_keys, machine_id)
            result["collection_id"] = existing["ratingKey"]
        else:
            result["collection_id"] = await self.create_collection(
                section_id, title, rating_keys, machine_id, media_type
            )
            result["created"] = bool(result["collection_id"])
        return result