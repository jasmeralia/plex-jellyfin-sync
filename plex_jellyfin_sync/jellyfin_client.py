from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
import time
from typing import Any

import requests

from plex_jellyfin_sync.models import DesiredMetadata, JellyfinCollection, JellyfinItem, JellyfinUserData, PersonRef


class JellyfinClientError(RuntimeError):
    """Raised when Jellyfin requests fail."""


class JellyfinNotFoundError(JellyfinClientError):
    """Raised when a Jellyfin resource does not exist."""


def _coerce_people(raw_people: list[dict[str, Any]] | None) -> tuple[PersonRef, ...]:
    people: list[PersonRef] = []
    for person in raw_people or []:
        name = person.get("Name")
        role = person.get("Type")
        if name and role:
            people.append(PersonRef(name=str(name), role=str(role)))
    return tuple(people)


def _coerce_studios(raw_studios: list[dict[str, Any]] | None) -> tuple[str, ...]:
    return tuple(str(studio["Name"]) for studio in raw_studios or [] if studio.get("Name"))


def _coerce_media_sources(raw_sources: list[dict[str, Any]] | None) -> tuple[tuple[str, str], ...]:
    media_sources: list[tuple[str, str]] = []
    for source in raw_sources or []:
        source_id = source.get("Id")
        path = source.get("Path")
        if source_id and path:
            media_sources.append((str(path), str(source_id)))
    return tuple(media_sources)


class JellyfinClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        library_name: str,
        user_id: str | None = None,
        session: requests.Session | None = None,
        request_timeout_seconds: float = 15.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._library_name = library_name
        self._user_id = user_id
        self._session = session or requests.Session()
        self._session.headers.update({"X-Emby-Token": api_key})
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep_func or time.sleep
        self._cached_library_id: str | None = None

    async def ready(self) -> bool:
        try:
            self._request("GET", "/System/Info")
            return True
        except JellyfinClientError:
            return False

    def list_library_items(self) -> list[JellyfinItem]:
        payload = self._request("GET", "/Items", params=self._item_fields_query(ParentId=self._library_id(), Recursive="true"))
        return [self._normalize_item(item) for item in payload.get("Items", [])]

    def get_item(self, item_id: str) -> JellyfinItem:
        payload = self._get_item_payload(item_id)
        return self._normalize_item(payload)

    def get_item_or_none(self, item_id: str) -> JellyfinItem | None:
        try:
            return self.get_item(item_id)
        except JellyfinNotFoundError:
            return None

    def find_item_by_path(self, path: str) -> JellyfinItem | None:
        payload = self._request("GET", "/Items", params=self._item_fields_query(ParentId=self._library_id(), Recursive="true"))
        for item in payload.get("Items", []):
            if item.get("Path") == path:
                return self._normalize_item(item)
        return None

    def list_collections(self) -> list[JellyfinCollection]:
        payload = self._request("GET", "/Items", params={"IncludeItemTypes": "BoxSet", "Recursive": "true"})
        collections: list[JellyfinCollection] = []
        for item in payload.get("Items", []):
            collection_id = item.get("Id")
            name = item.get("Name")
            if collection_id and name:
                collections.append(
                    JellyfinCollection(
                        collection_id=str(collection_id),
                        name=str(name),
                        item_ids=self.get_collection_item_ids(str(collection_id)),
                    )
                )
        return collections

    def get_collection_item_ids(self, collection_id: str) -> tuple[str, ...]:
        payload = self._request("GET", "/Items", params={"ParentId": collection_id, "Recursive": "true"})
        return tuple(str(item["Id"]) for item in payload.get("Items", []) if item.get("Id"))

    def create_collection(self, name: str, item_ids: Iterable[str]) -> str:
        ids = ",".join(item_ids)
        payload = self._request("POST", "/Collections", params={"name": name, "ids": ids})
        created = payload.get("Id")
        if created:
            return str(created)
        items = payload.get("Items") or []
        if items and items[0].get("Id"):
            return str(items[0]["Id"])
        for collection in self.list_collections():
            if collection.name == name:
                return collection.collection_id
        raise JellyfinClientError(f"Unable to resolve created collection {name!r}")

    def add_items_to_collection(self, collection_id: str, item_ids: Iterable[str]) -> None:
        ids = ",".join(item_ids)
        if ids:
            self._request("POST", f"/Collections/{collection_id}/Items", params={"ids": ids})

    def remove_items_from_collection(self, collection_id: str, item_ids: Iterable[str]) -> None:
        ids = ",".join(item_ids)
        if ids:
            self._request("DELETE", f"/Collections/{collection_id}/Items", params={"ids": ids})

    def rename_collection(self, collection_id: str, name: str) -> None:
        body = self._editable_item_payload(collection_id)
        body["Name"] = name
        self._request("POST", f"/Items/{collection_id}", json=body)

    def delete_item(self, item_id: str) -> None:
        self._request("DELETE", f"/Items/{item_id}")

    def find_person_id_by_name(self, name: str) -> str | None:
        payload = self._request("GET", "/Persons", params={"searchTerm": name})
        for person in payload.get("Items", []):
            person_id = person.get("Id")
            person_name = person.get("Name")
            if person_id and isinstance(person_name, str) and person_name.casefold() == name.casefold():
                return str(person_id)
        return None

    def update_item_metadata(self, item_id: str, metadata: DesiredMetadata) -> None:
        body = self._editable_item_payload(item_id)
        body["Studios"] = [{"Name": name} for name in metadata.studios]
        body["People"] = [{"Name": person.name, "Type": person.role} for person in metadata.people]
        body["LockedFields"] = list(metadata.locked_fields)
        body["LockData"] = bool(metadata.locked_fields)
        self._request("POST", f"/Items/{item_id}", json=body)

    def get_user_data(self, user_id: str, item_id: str) -> JellyfinUserData:
        payload = self._request("GET", f"/Users/{user_id}/Items/{item_id}", params={"Fields": "UserData"})
        user_data = payload.get("UserData") or {}
        last_played = user_data.get("LastPlayedDate")
        return JellyfinUserData(
            played=bool(user_data.get("Played", False)),
            play_count=int(user_data.get("PlayCount", 0) or 0),
            rating=float(user_data["Rating"]) if user_data.get("Rating") is not None else None,
            last_played_date=datetime.fromisoformat(last_played) if isinstance(last_played, str) else None,
        )

    def mark_played(self, user_id: str, item_id: str) -> None:
        self._request("POST", f"/Users/{user_id}/PlayedItems/{item_id}")

    def mark_unplayed(self, user_id: str, item_id: str) -> None:
        self._request("DELETE", f"/Users/{user_id}/PlayedItems/{item_id}")

    def update_user_data(
        self,
        user_id: str,
        item_id: str,
        *,
        play_count: int | None,
        rating: float | None,
        last_played_date: datetime | None,
    ) -> None:
        payload: dict[str, Any] = {}
        if play_count is not None:
            payload["PlayCount"] = play_count
        if rating is not None:
            payload["Rating"] = rating
        if last_played_date is not None:
            payload["LastPlayedDate"] = last_played_date.isoformat()
        if payload:
            self._request("POST", f"/Users/{user_id}/Items/{item_id}/UserData", json=payload)

    def trigger_library_refresh(self) -> None:
        self._request("POST", "/Library/Refresh")

    def merge_versions(self, ordered_ids: Iterable[str]) -> None:
        ids = ",".join(ordered_ids)
        self._request("POST", "/Videos/MergeVersions", params={"ids": ids})

    def unmerge_versions(self, primary_id: str) -> None:
        self._request("DELETE", f"/Videos/{primary_id}/AlternateSources")

    @staticmethod
    def _item_fields_query(**params: str) -> dict[str, str]:
        return {
            **params,
            "Fields": "Path,People,Studios,LockedFields,MediaSources",
        }

    @staticmethod
    def _editable_item_fields_query(**params: str) -> dict[str, str]:
        return {
            **params,
            "Fields": "Path,People,Studios,LockedFields,MediaSources,Genres,Tags,ProviderIds,ProductionLocations,Taglines",
        }

    def _get_item_payload(self, item_id: str) -> dict[str, Any]:
        if self._user_id:
            return self._request(
                "GET",
                f"/Users/{self._user_id}/Items/{item_id}",
                params={"Fields": "Path,People,Studios,LockedFields,MediaSources"},
            )
        payload = self._request("GET", "/Items", params=self._item_fields_query(Ids=item_id))
        items = payload.get("Items", [])
        if items:
            return items[0]
        raise JellyfinNotFoundError(f"Item {item_id!r} was not returned by Jellyfin")

    def _editable_item_payload(self, item_id: str) -> dict[str, Any]:
        payload = self._request("GET", "/Items", params=self._editable_item_fields_query(Ids=item_id))
        items = payload.get("Items", [])
        if not items:
            raise JellyfinNotFoundError(f"Item {item_id!r} was not returned by Jellyfin")
        item = dict(items[0])
        item.setdefault("Name", item_id)
        item.setdefault("Genres", [])
        item.setdefault("Tags", [])
        item["ProviderIds"] = dict(item.get("ProviderIds") or {})
        item.setdefault("ProductionLocations", [])
        item.setdefault("Taglines", [])
        item.setdefault("People", [])
        item.setdefault("Studios", [])
        item.setdefault("LockedFields", [])
        return item

    def _normalize_item(self, payload: dict[str, Any]) -> JellyfinItem:
        metadata = DesiredMetadata(
            studios=_coerce_studios(payload.get("Studios")),
            people=_coerce_people(payload.get("People")),
            locked_fields=tuple(str(field) for field in payload.get("LockedFields", []) or []),
        )
        return JellyfinItem(
            item_id=str(payload["Id"]),
            path=str(payload.get("Path", "")),
            metadata=metadata,
            media_sources=_coerce_media_sources(payload.get("MediaSources")),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: requests.RequestException | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    json=json,
                    timeout=self._request_timeout_seconds,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code == 404:
                    raise JellyfinNotFoundError(str(exc)) from exc
                last_error = exc
                should_retry = status_code is None or int(status_code) >= 500
                if not should_retry or attempt >= self._max_retries:
                    raise JellyfinClientError(str(exc)) from exc
                self._sleep(self._retry_backoff_seconds * (2 ** attempt))
                continue
            else:
                if not response.content:
                    return {}
                return response.json()

        if last_error is not None:  # pragma: no cover - defensive fallback
            raise JellyfinClientError(str(last_error)) from last_error
        raise JellyfinClientError("Jellyfin request failed without a response")

    def _library_id(self) -> str:
        if self._cached_library_id is not None:
            return self._cached_library_id
        payload = self._request("GET", "/Library/VirtualFolders")
        folders = payload if isinstance(payload, list) else payload.get("Items", [])
        for folder in folders:
            name = folder.get("Name")
            folder_id = folder.get("ItemId") or folder.get("Id")
            if name == self._library_name and folder_id:
                self._cached_library_id = str(folder_id)
                return self._cached_library_id
        raise JellyfinClientError(f"Unable to resolve Jellyfin library {self._library_name!r}")
