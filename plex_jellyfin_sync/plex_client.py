from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import time
from typing import Any, TypeVar

import requests

from plex_jellyfin_sync.models import PlexCollection, PlexItem, PlexUserData

try:  # pragma: no cover - import availability varies by environment
    from plexapi.server import PlexServer as PlexServerType
except ImportError:  # pragma: no cover - handled at runtime
    PlexServerType = None


T = TypeVar("T")


class PlexClientError(RuntimeError):
    """Raised when Plex requests fail."""


class PlexClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        library_name: str,
        server_factory: Callable[[str, str], Any] | None = None,
        request_timeout_seconds: float = 15.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
        sleep_func: Callable[[float], None] | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._library_name = library_name
        self._server_factory = server_factory or self._default_server_factory
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep_func or time.sleep

    async def ready(self) -> bool:
        try:
            self._section()
            return True
        except Exception:
            return False

    def get_item(self, rating_key: int) -> PlexItem | None:
        section = self._section()
        item = self._get_item_from_section(section, rating_key)
        if item is None:
            return None
        return self._normalize_item(item)

    def list_items(self) -> list[PlexItem]:
        items = self._retry(lambda: self._section().all())
        return [self._normalize_item(item) for item in items]

    def list_collections(self) -> list[PlexCollection]:
        raw_collections = self._retry(lambda: self._section().collections())
        collections: list[PlexCollection] = []
        for collection in raw_collections:
            members = tuple(int(item.ratingKey) for item in self._retry(collection.items))
            collections.append(
                PlexCollection(
                    key=int(collection.ratingKey),
                    name=str(collection.title),
                    member_rating_keys=members,
                )
            )
        return collections

    def get_user_data(self, rating_key: int, *, token: str | None = None) -> PlexUserData | None:
        section = self._section(token=token)
        item = self._get_item_from_section(section, rating_key)
        if item is None:
            return None
        return PlexUserData(
            watched=bool(getattr(item, "isWatched", False)),
            play_count=int(getattr(item, "viewCount", 0) or 0),
            rating=float(getattr(item, "userRating", 0.0)) if getattr(item, "userRating", None) is not None else None,
            last_viewed_at=self._coerce_datetime(getattr(item, "lastViewedAt", None)),
        )

    def _get_item_from_section(self, section: Any, rating_key: int) -> Any | None:
        try:
            return self._retry(lambda: section.get(str(rating_key)))
        except KeyError:
            return None

    def _section(self, *, token: str | None = None) -> Any:
        try:
            server = self._retry(lambda: self._server_factory(self._base_url, token or self._token))
            return self._retry(lambda: server.library.section(self._library_name))
        except Exception as exc:  # pragma: no cover - adapter to external dependency
            raise PlexClientError(f"Unable to open Plex library {self._library_name!r}") from exc

    def _normalize_item(self, item: Any) -> PlexItem:
        paths = tuple(part.file for media in item.media for part in media.parts)
        writers = tuple(getattr(writer, "tag", str(writer)) for writer in getattr(item, "writers", []) or [])
        directors = tuple(getattr(director, "tag", str(director)) for director in getattr(item, "directors", []) or [])
        collections = tuple(getattr(collection, "tag", str(collection)) for collection in getattr(item, "collections", []) or [])
        studio = getattr(item, "studio", None)
        primary_path = paths[0] if paths else None
        return PlexItem(
            rating_key=int(item.ratingKey),
            path=primary_path or "",
            paths=paths,
            studio=studio,
            writers=writers,
            directors=directors,
            collections=collections,
            primary_path=primary_path,
        )

    def _default_server_factory(self, base_url: str, token: str) -> Any:
        if PlexServerType is None:  # pragma: no cover - depends on installed package
            raise PlexClientError("plexapi is not installed")
        return PlexServerType(base_url, token, timeout=self._request_timeout_seconds)

    def _coerce_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return None

    def _retry(self, operation: Callable[[], T]) -> T:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return operation()
            except KeyError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise PlexClientError(str(exc)) from exc
                self._sleep(self._retry_backoff_seconds * (2 ** attempt))
        if last_error is not None:  # pragma: no cover - defensive fallback
            raise PlexClientError(str(last_error)) from last_error
        raise PlexClientError("Plex request failed without an error")


class PlexWebhookVerifier:
    def __init__(self, shared_secret: str | None) -> None:
        self._shared_secret = shared_secret

    def is_valid(self, provided_secret: str | None) -> bool:
        if self._shared_secret is None:
            return True
        return provided_secret == self._shared_secret


def normalize_http_error(exc: requests.RequestException) -> PlexClientError:
    return PlexClientError(str(exc))
