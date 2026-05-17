from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, order=True)
class PersonRef:
    name: str
    role: str


@dataclass(frozen=True)
class DesiredMetadata:
    studios: tuple[str, ...] = ()
    people: tuple[PersonRef, ...] = ()
    locked_fields: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlexUserData:
    watched: bool = False
    play_count: int = 0
    rating: float | None = None
    last_viewed_at: datetime | None = None


@dataclass(frozen=True)
class JellyfinUserData:
    played: bool = False
    play_count: int = 0
    rating: float | None = None
    last_played_date: datetime | None = None


@dataclass(frozen=True)
class UserDataMergePlan:
    changed: bool = False
    mark_played: bool = False
    play_count: int | None = None
    rating: float | None = None
    last_played_date: datetime | None = None


@dataclass(frozen=True)
class MetadataDiff:
    update_item: bool = False
    people_to_add: tuple[PersonRef, ...] = ()
    people_to_remove: tuple[PersonRef, ...] = ()


@dataclass(frozen=True)
class MergePlan:
    action: str
    primary_id: str | None = None
    ordered_ids: tuple[str, ...] = ()
    unresolved_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncEvent:
    kind: str
    source: str
    rating_key: int | None = None
    plex_account: str | None = None
    jellyfin_user_id: str | None = None
    requeue_count: int = 0
    job_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlexCollection:
    key: int
    name: str
    member_rating_keys: tuple[int, ...] = ()


@dataclass(frozen=True)
class PlexItem:
    rating_key: int
    path: str
    paths: tuple[str, ...]
    studio: str | None = None
    writers: tuple[str, ...] = ()
    directors: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    primary_path: str | None = None

    def __post_init__(self) -> None:
        if not self.paths:
            object.__setattr__(self, "paths", (self.path,))
        if self.primary_path is None:
            object.__setattr__(self, "primary_path", self.paths[0])


@dataclass(frozen=True)
class JellyfinItem:
    item_id: str
    path: str
    metadata: DesiredMetadata = field(default_factory=DesiredMetadata)
    media_sources: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class JellyfinCollection:
    collection_id: str
    name: str
    item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemMapRecord:
    plex_rating_key: int
    jellyfin_primary_id: str
    is_merged: bool
    content_hash: str
    last_synced_at: datetime


@dataclass(frozen=True)
class MediaSourceRecord:
    path: str
    plex_rating_key: int
    jellyfin_source_id: str
    jellyfin_primary_id: str
    is_primary: bool


@dataclass(frozen=True)
class CollectionMapRecord:
    plex_collection_key: int
    jellyfin_id: str
    name: str
    last_synced_at: datetime


@dataclass(frozen=True)
class UserDataMapRecord:
    plex_rating_key: int
    jellyfin_user_id: str
    last_plex_viewcount: int
    last_plex_watched: bool
    last_plex_rating: float | None
    last_plex_lastviewed: datetime | None
    last_synced_at: datetime


@dataclass(frozen=True)
class SyncLogRecord:
    trigger: str
    scope: str
    started_at: datetime
    completed_at: datetime | None = None
    items_examined: int = 0
    items_updated: int = 0
    user_data_updated: int = 0
    merges_applied: int = 0
    unmerges_applied: int = 0
    requeued_events: int = 0
    errors: int = 0
    error_detail: str | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)
