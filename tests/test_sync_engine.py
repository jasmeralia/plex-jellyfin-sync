from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from plex_jellyfin_sync.config import AppConfig
from plex_jellyfin_sync.diff import compute_content_hash
from plex_jellyfin_sync.models import (
    CollectionMapRecord,
    DesiredMetadata,
    ItemMapRecord,
    JellyfinCollection,
    JellyfinItem,
    JellyfinUserData,
    MediaSourceRecord,
    PlexCollection,
    PlexItem,
    PlexUserData,
    SyncEvent,
)
from plex_jellyfin_sync.state import StateStore
from plex_jellyfin_sync.sync_engine import SyncEngine


class FakePlexClient:
    def __init__(self, item: PlexItem | None, user_data: PlexUserData, collections: list[PlexCollection] | None = None) -> None:
        self.item = item
        self.user_data = user_data
        self.collections = collections or []

    def list_items(self):
        return [] if self.item is None else [self.item]

    def get_item(self, rating_key: int):
        if self.item is None:
            return None
        return self.item if rating_key == self.item.rating_key else None

    def get_user_data(self, rating_key: int, *, token: str | None = None):
        assert self.item is not None
        assert rating_key == self.item.rating_key
        return self.user_data

    def list_collections(self):
        return self.collections


class MultiItemFakePlexClient:
    def __init__(
        self,
        items: list[PlexItem],
        user_data_by_rating_key: dict[int, PlexUserData] | None = None,
        collections: list[PlexCollection] | None = None,
    ) -> None:
        self.items = {item.rating_key: item for item in items}
        self.user_data_by_rating_key = user_data_by_rating_key or {}
        self.collections = collections or []

    def list_items(self):
        return list(self.items.values())

    def get_item(self, rating_key: int):
        return self.items.get(rating_key)

    def get_user_data(self, rating_key: int, *, token: str | None = None):
        return self.user_data_by_rating_key.get(rating_key, PlexUserData())

    def list_collections(self):
        return self.collections


class FailingLookupPlexClient(FakePlexClient):
    def get_item(self, rating_key: int):
        raise RuntimeError(f"temporary plex lookup failure for {rating_key}")


class FakeJellyfinClient:
    def __init__(self) -> None:
        self.by_path = {
            "/media/a.mkv": JellyfinItem("item-1", "/media/a.mkv", DesiredMetadata()),
        }
        self.by_id = {
            "item-1": JellyfinItem("item-1", "/media/a.mkv", DesiredMetadata()),
        }
        self.collections = {}
        self.updated_metadata = []
        self.played_calls = []
        self.unplayed_calls = []
        self.user_data_reads = []
        self.updated_user_data = []
        self.collection_adds = []
        self.collection_removes = []
        self.collection_creates = []
        self.collection_renames = []
        self.deleted_items = []
        self.merge_calls = []
        self.unmerge_calls = []
        self.library_item_counts = [1]
        self.refreshed = False
        self.person_ids = {"Alice": "person-alice", "Bob": "person-bob"}
        self.person_lookup_calls = []

    def trigger_library_refresh(self) -> None:
        self.refreshed = True

    def list_library_items(self):
        count = self.library_item_counts.pop(0) if len(self.library_item_counts) > 1 else self.library_item_counts[0]
        return [self.by_id["item-1"]] * count

    def find_item_by_path(self, path: str):
        return self.by_path.get(path)

    def get_item(self, item_id: str):
        return self.by_id[item_id]

    def get_item_or_none(self, item_id: str):
        return self.by_id.get(item_id)

    def update_item_metadata(self, item_id: str, metadata: DesiredMetadata) -> None:
        self.updated_metadata.append((item_id, metadata))
        self.by_id[item_id] = JellyfinItem(item_id, self.by_id[item_id].path, metadata)

    def get_user_data(self, user_id: str, item_id: str):
        self.user_data_reads.append((user_id, item_id))
        return JellyfinUserData(played=False, play_count=0, rating=None, last_played_date=None)

    def mark_played(self, user_id: str, item_id: str) -> None:
        self.played_calls.append((user_id, item_id))

    def mark_unplayed(self, user_id: str, item_id: str) -> None:
        self.unplayed_calls.append((user_id, item_id))

    def update_user_data(self, user_id: str, item_id: str, *, play_count, rating, last_played_date) -> None:
        self.updated_user_data.append((user_id, item_id, play_count, rating, last_played_date))

    def merge_versions(self, ordered_ids) -> None:
        self.merge_calls.append(tuple(ordered_ids))

    def unmerge_versions(self, primary_id: str) -> None:
        self.unmerge_calls.append(primary_id)
        primary_item = self.by_id.get(primary_id)
        if primary_item is None or not primary_item.media_sources:
            return
        for path, _source_id in primary_item.media_sources:
            self.by_path.pop(path, None)
        self.by_id.pop(primary_id, None)
        for path, source_id in primary_item.media_sources:
            split_item = JellyfinItem(source_id, path, primary_item.metadata)
            self.by_id[source_id] = split_item
            self.by_path[path] = split_item

    def list_collections(self):
        return list(self.collections.values())

    def create_collection(self, name: str, item_ids):
        collection_id = f"box-{len(self.collections) + 1}"
        collection = JellyfinCollection(collection_id, name, tuple(item_ids))
        self.collections[name] = collection
        self.collection_creates.append((name, tuple(item_ids)))
        return collection_id

    def add_items_to_collection(self, collection_id: str, item_ids):
        self.collection_adds.append((collection_id, tuple(item_ids)))

    def remove_items_from_collection(self, collection_id: str, item_ids):
        self.collection_removes.append((collection_id, tuple(item_ids)))

    def rename_collection(self, collection_id: str, name: str) -> None:
        self.collection_renames.append((collection_id, name))
        for current_name, collection in list(self.collections.items()):
            if collection.collection_id == collection_id:
                del self.collections[current_name]
                self.collections[name] = JellyfinCollection(collection_id, name, collection.item_ids)
                return

    def delete_item(self, item_id: str) -> None:
        self.deleted_items.append(item_id)
        for name, collection in list(self.collections.items()):
            if collection.collection_id == item_id:
                del self.collections[name]

    def find_person_id_by_name(self, name: str) -> str | None:
        self.person_lookup_calls.append(name)
        return self.person_ids.get(name)


class FailingUserDataJellyfinClient(FakeJellyfinClient):
    def get_user_data(self, user_id: str, item_id: str):
        raise RuntimeError("user data endpoint failed")


class DistinctSourceIdMergedJellyfinClient(FakeJellyfinClient):
    def __init__(self) -> None:
        super().__init__()
        merged_item = JellyfinItem(
            "item-1",
            "/media/a.mkv",
            DesiredMetadata(locked_fields=("Cast", "Studios")),
            media_sources=(("/media/a.mkv", "src-a"), ("/media/b.mkv", "src-b")),
        )
        self.by_id = {"item-1": merged_item}
        self.by_path = {"/media/a.mkv": merged_item}


class FakeLogger:
    def __init__(self) -> None:
        self.info_calls: list[tuple[str, dict]] = []
        self.warning_calls: list[tuple[str, dict]] = []
        self.exception_calls: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs) -> None:
        self.info_calls.append((event, kwargs))

    def warning(self, event: str, **kwargs) -> None:
        self.warning_calls.append((event, kwargs))

    def exception(self, event: str, **kwargs) -> None:
        self.exception_calls.append((event, kwargs))


def build_config(tmp_path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "plex": {
                "base_url": "http://plex:32400",
                "token": "plex-token",
                "library_name": "Other Video",
            },
            "jellyfin": {
                "base_url": "http://jellyfin:8096",
                "api_key": "jf-key",
                "user_id": "admin-id",
                "library_name": "Other Video",
            },
            "user_mapping": [
                {
                    "plex_account": "jas",
                    "plex_token": None,
                    "jellyfin_user_id": "jf-user-1",
                }
            ],
            "state": {"sqlite_path": str(tmp_path / "sync.db")},
        }
    )


async def test_sync_engine_item_event_persists_mapping_and_updates_metadata(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        studio="Studio A",
        writers=("Alice",),
        directors=("Bob",),
        collections=("Favorites",),
    )
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=FakeJellyfinClient(),
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert result.items_updated == 1
    item_map = state.get_item_map(42)
    assert item_map is not None
    assert item_map.jellyfin_primary_id == "item-1"
    assert state.get_media_source_by_path("/media/a.mkv") is not None


async def test_sync_engine_item_event_also_syncs_collections(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        collections=("Favorites",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(),
            collections=[PlexCollection(key=10, name="Favorites", member_rating_keys=(42,))],
        ),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.collection_creates == [("Favorites", ("item-1",))]


async def test_sync_engine_renames_existing_collection_when_plex_name_changes(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    state.upsert_collection_map(CollectionMapRecord(10, "box-1", "Old Favorites", datetime(2024, 1, 1, tzinfo=UTC)))
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        collections=("Favorites",),
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.collections["Old Favorites"] = JellyfinCollection("box-1", "Old Favorites", ("item-1",))
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(),
            collections=[PlexCollection(key=10, name="Favorites", member_rating_keys=(42,))],
        ),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.collection_renames == [("box-1", "Favorites")]
    collection_map = state.list_collection_maps()
    assert collection_map[0].name == "Favorites"


async def test_sync_engine_skips_collection_updates_until_all_members_are_mapped(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    state.upsert_collection_map(CollectionMapRecord(10, "box-1", "Favorites", datetime(2024, 1, 1, tzinfo=UTC)))
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        collections=("Favorites",),
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.collections["Favorites"] = JellyfinCollection("box-1", "Favorites", ("item-1", "item-2"))
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(),
            collections=[PlexCollection(key=10, name="Favorites", member_rating_keys=(42, 99))],
        ),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.collection_creates == []
    assert jellyfin.collection_adds == []
    assert jellyfin.collection_removes == []
    assert jellyfin.deleted_items == []
    assert jellyfin.collections["Favorites"].item_ids == ("item-1", "item-2")


async def test_sync_engine_respects_disabled_collection_field_mapping(tmp_path) -> None:
    config = build_config(tmp_path)
    config.sync.field_mapping.collections = False
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        collections=("Favorites",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(),
            collections=[PlexCollection(key=10, name="Favorites", member_rating_keys=(42,))],
        ),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.collection_creates == []


async def test_sync_engine_respects_disabled_user_data_sync(tmp_path) -> None:
    config = build_config(tmp_path)
    config.sync.user_data.watched = False
    config.sync.user_data.play_count = False
    config.sync.user_data.rating = False
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(
        SyncEvent(kind="userdata", source="webhook", rating_key=42, jellyfin_user_id="jf-user-1")
    )

    assert result.user_data_updated == 0
    assert jellyfin.played_calls == []
    assert jellyfin.updated_user_data == []
    assert state.get_user_data_map(42, "jf-user-1") is None


async def test_sync_engine_item_event_survives_user_data_failure(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        studio="Studio A",
    )
    jellyfin = FailingUserDataJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))
    sync_log = state.get_sync_log(1)

    assert result.items_updated == 1
    assert result.user_data_updated == 0
    assert result.errors == 1
    assert state.get_item_map(42) is not None
    assert sync_log is not None
    assert "user-data sync failed" in json.loads(sync_log.error_detail)[0]


async def test_sync_engine_userdata_event_records_failure_without_raising(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FailingUserDataJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(
        SyncEvent(kind="userdata", source="webhook", rating_key=42, jellyfin_user_id="jf-user-1")
    )
    sync_log = state.get_sync_log(1)

    assert result.user_data_updated == 0
    assert result.errors == 1
    assert sync_log is not None
    assert "user-data sync failed" in json.loads(sync_log.error_detail)[0]


async def test_sync_engine_respects_disabled_studio_writer_director_fields(tmp_path) -> None:
    config = build_config(tmp_path)
    config.sync.field_mapping.studio = False
    config.sync.field_mapping.writers_as_actors = False
    config.sync.field_mapping.directors = False
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        studio="Studio A",
        writers=("Alice",),
        directors=("Bob",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.updated_metadata[0][1].studios == ()
    assert jellyfin.updated_metadata[0][1].people == ()


async def test_sync_engine_populates_person_cache_on_metadata_sync(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        writers=("Alice",),
        directors=("Bob",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert state.get_person_cache("Alice") == "person-alice"
    assert state.get_person_cache("Bob") == "person-bob"
    assert jellyfin.person_lookup_calls == ["Alice", "Bob", "Alice", "Bob"]


async def test_sync_engine_skips_person_lookup_when_cache_is_warm(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    state.upsert_person_cache(name="Alice", jellyfin_id="person-alice")
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        writers=("Alice",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.person_lookup_calls == ["Alice"]


async def test_sync_engine_does_not_skip_merge_changes_when_content_hash_is_unchanged(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv"),
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.by_path["/media/b.mkv"] = JellyfinItem("item-2", "/media/b.mkv", DesiredMetadata())
    jellyfin.by_id["item-2"] = JellyfinItem("item-2", "/media/b.mkv", DesiredMetadata())
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=False,
            content_hash=compute_content_hash(DesiredMetadata(locked_fields=("Cast", "Studios"))),
            last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-1",
                jellyfin_primary_id="item-1",
                is_primary=True,
            )
        ],
    )
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert result.items_updated == 1
    assert jellyfin.merge_calls == [("item-1", "item-2")]
    assert {record.path for record in state.get_media_sources_for_rating_key(42)} == {"/media/a.mkv", "/media/b.mkv"}


async def test_sync_engine_keeps_existing_merged_group_when_media_source_ids_differ_from_item_ids(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv"),
    )
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=True,
            content_hash=compute_content_hash(DesiredMetadata(locked_fields=("Cast", "Studios"))),
            last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="src-a",
                jellyfin_primary_id="item-1",
                is_primary=True,
            ),
            MediaSourceRecord(
                path="/media/b.mkv",
                plex_rating_key=42,
                jellyfin_source_id="src-b",
                jellyfin_primary_id="item-1",
                is_primary=False,
            ),
        ],
    )
    jellyfin = DistinctSourceIdMergedJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert result.items_updated == 0
    assert result.merges_applied == 0
    assert result.unmerges_applied == 0
    assert jellyfin.merge_calls == []
    assert jellyfin.unmerge_calls == []


async def test_sync_engine_respects_disabled_merging_for_multifile_items(tmp_path) -> None:
    config = build_config(tmp_path)
    config.sync.merging.enabled = False
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv"),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert result.items_updated == 1
    assert result.merges_applied == 0
    assert result.unmerges_applied == 0
    assert jellyfin.merge_calls == []
    assert jellyfin.unmerge_calls == []
    item_map = state.get_item_map(42)
    assert item_map is not None
    assert item_map.is_merged is False
    assert {record.path for record in state.get_media_sources_for_rating_key(42)} == {"/media/a.mkv"}


async def test_sync_engine_overwrites_jellyfin_metadata_drift_even_when_content_hash_matches(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
        studio="Studio A",
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.by_id["item-1"] = JellyfinItem(
        "item-1",
        "/media/a.mkv",
        DesiredMetadata(studios=("Wrong Studio",), locked_fields=("Cast", "Studios")),
    )
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=False,
            content_hash=compute_content_hash(DesiredMetadata(studios=("Studio A",), locked_fields=("Cast", "Studios"))),
            last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-1",
                jellyfin_primary_id="item-1",
                is_primary=True,
            )
        ],
    )
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert result.items_updated == 1
    assert jellyfin.updated_metadata[-1][1].studios == ("Studio A",)


async def test_sync_engine_user_data_event_marks_played_and_updates_state(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(
        SyncEvent(kind="userdata", source="webhook", rating_key=42, jellyfin_user_id="jf-user-1")
    )

    assert result.user_data_updated == 1
    assert jellyfin.played_calls == [("jf-user-1", "item-1")]
    assert jellyfin.updated_user_data[0][2] == 2
    assert state.get_user_data_map(42, "jf-user-1") is not None


async def test_sync_engine_logs_userdata_changes_on_success(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )
    logger = FakeLogger()
    engine._logger = logger

    await engine.handle_event(
        SyncEvent(kind="userdata", source="webhook", rating_key=42, jellyfin_user_id="jf-user-1")
    )

    userdata_log = next(kwargs for event, kwargs in logger.info_calls if event == "sync.userdata_updated")
    assert userdata_log["plex_rating_key"] == 42
    assert userdata_log["jellyfin_user_id"] == "jf-user-1"
    assert userdata_log["userdata_changes"] == ["watched", "play_count", "rating", "last_played"]


async def test_sync_engine_full_sync_refreshes_library(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert jellyfin.refreshed is True
    assert result.items_examined == 1


async def test_sync_engine_full_sync_syncs_user_data_for_each_mapped_user(tmp_path) -> None:
    config = build_config(tmp_path)
    config.user_mapping.append(
        config.user_mapping[0].model_copy(
            update={"plex_account": "alex", "jellyfin_user_id": "jf-user-2"}
        )
    )
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert result.user_data_updated == 2
    assert jellyfin.played_calls == [("jf-user-1", "item-1"), ("jf-user-2", "item-1")]
    assert [call[0] for call in jellyfin.updated_user_data] == ["jf-user-1", "jf-user-2"]


async def test_sync_engine_prunes_mapping_when_item_is_gone_from_plex_and_jellyfin(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=False,
            content_hash="hash",
            last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-1",
                jellyfin_primary_id="item-1",
                is_primary=True,
            )
        ],
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.by_id.pop("item-1", None)
    jellyfin.by_path.pop("/media/a.mkv", None)
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(None, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert state.get_item_map(42) is None
    assert state.get_media_sources_for_rating_key(42) == []


async def test_sync_engine_full_sync_waits_for_library_refresh(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.library_item_counts = [0, 0, 1]
    sleeps = []
    ticks = iter([0.0, 0.1, 1.1, 2.1])
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
        sleep_func=lambda seconds: sleeps.append(seconds),
        monotonic_func=lambda: next(ticks),
    )

    result = await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert result.errors == 0
    assert sleeps == [1.0, 1.0]


async def test_sync_engine_full_sync_logs_refresh_timeout(tmp_path) -> None:
    config = build_config(tmp_path)
    config.sync.merging.refresh_timeout_seconds = 0
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.library_item_counts = [0]
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
        monotonic_func=lambda: 0.0,
    )

    result = await engine.handle_event(SyncEvent(kind="full", source="manual"))
    sync_log = state.get_sync_log(1)

    assert result.errors == 1
    assert sync_log is not None
    assert "timed out" in json.loads(sync_log.error_detail)[0]


async def test_sync_engine_full_sync_materializes_collections(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(),
            collections=[PlexCollection(key=10, name="Favorites", member_rating_keys=(42,))],
        ),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert jellyfin.collection_creates == [("Favorites", ("item-1",))]
    collection_map = state.list_collection_maps()
    assert collection_map[0].name == "Favorites"


async def test_sync_engine_full_sync_unmerges_stale_group_when_paths_move_to_new_rating_keys(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=True,
            content_hash="old-hash",
            last_synced_at=now,
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-1",
                jellyfin_primary_id="item-1",
                is_primary=True,
            ),
            MediaSourceRecord(
                path="/media/b.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-2",
                jellyfin_primary_id="item-1",
                is_primary=False,
            ),
        ],
    )

    jellyfin = FakeJellyfinClient()
    jellyfin.by_id["item-1"] = JellyfinItem(
        "item-1",
        "/media/a.mkv",
        DesiredMetadata(),
        media_sources=(("/media/a.mkv", "item-1"), ("/media/b.mkv", "item-2")),
    )
    jellyfin.by_path = {"/media/a.mkv": jellyfin.by_id["item-1"]}
    jellyfin.library_item_counts = [2]
    plex_items = [
        PlexItem(rating_key=100, path="/media/a.mkv", paths=("/media/a.mkv",)),
        PlexItem(rating_key=101, path="/media/b.mkv", paths=("/media/b.mkv",)),
    ]
    engine = SyncEngine(
        config=config,
        state=state,
        plex=MultiItemFakePlexClient(plex_items),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert result.items_examined == 2
    assert jellyfin.unmerge_calls == ["item-1"]
    assert state.get_item_map(42) is None
    assert state.get_item_map(100).jellyfin_primary_id == "item-1"
    assert state.get_item_map(101).jellyfin_primary_id == "item-2"


async def test_sync_engine_rebuilds_merged_group_when_current_primary_is_wrong(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-2",
            is_merged=True,
            content_hash="old-hash",
            last_synced_at=now,
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-1",
                jellyfin_primary_id="item-2",
                is_primary=False,
            ),
            MediaSourceRecord(
                path="/media/b.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-2",
                jellyfin_primary_id="item-2",
                is_primary=True,
            ),
        ],
    )
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv"),
    )
    jellyfin = FakeJellyfinClient()
    wrong_primary = JellyfinItem(
        "item-2",
        "/media/b.mkv",
        DesiredMetadata(),
        media_sources=(("/media/b.mkv", "item-2"), ("/media/a.mkv", "item-1")),
    )
    jellyfin.by_id = {"item-2": wrong_primary}
    jellyfin.by_path = {"/media/b.mkv": wrong_primary}
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert result.merges_applied == 1
    assert result.unmerges_applied == 1
    assert jellyfin.unmerge_calls == ["item-2"]
    assert jellyfin.merge_calls == [("item-1", "item-2")]
    item_map = state.get_item_map(42)
    assert item_map is not None
    assert item_map.jellyfin_primary_id == "item-1"


async def test_sync_engine_updates_metadata_once_on_three_file_merge_and_targets_primary(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv", "/media/c.mkv"),
        studio="Studio A",
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.by_path["/media/b.mkv"] = JellyfinItem("item-2", "/media/b.mkv", DesiredMetadata())
    jellyfin.by_path["/media/c.mkv"] = JellyfinItem("item-3", "/media/c.mkv", DesiredMetadata())
    jellyfin.by_id["item-2"] = JellyfinItem("item-2", "/media/b.mkv", DesiredMetadata())
    jellyfin.by_id["item-3"] = JellyfinItem("item-3", "/media/c.mkv", DesiredMetadata())
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.merge_calls == [("item-1", "item-2", "item-3")]
    assert [item_id for item_id, _metadata in jellyfin.updated_metadata] == ["item-1"]


async def test_sync_engine_disabled_merging_unmerges_existing_group_and_tracks_primary_only(tmp_path) -> None:
    config = build_config(tmp_path)
    config.sync.merging.enabled = False
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=True,
            content_hash="old-hash",
            last_synced_at=now,
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-1",
                jellyfin_primary_id="item-1",
                is_primary=True,
            ),
            MediaSourceRecord(
                path="/media/b.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-2",
                jellyfin_primary_id="item-1",
                is_primary=False,
            ),
        ],
    )
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv"),
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.by_id["item-1"] = JellyfinItem(
        "item-1",
        "/media/a.mkv",
        DesiredMetadata(),
        media_sources=(("/media/a.mkv", "item-1"), ("/media/b.mkv", "item-2")),
    )
    jellyfin.by_path = {"/media/a.mkv": jellyfin.by_id["item-1"]}
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert result.items_updated == 1
    assert result.merges_applied == 0
    assert result.unmerges_applied == 1
    assert jellyfin.merge_calls == []
    assert jellyfin.unmerge_calls == ["item-1"]
    item_map = state.get_item_map(42)
    assert item_map is not None
    assert item_map.jellyfin_primary_id == "item-1"
    assert item_map.is_merged is False
    assert {record.path for record in state.get_media_sources_for_rating_key(42)} == {"/media/a.mkv"}


async def test_sync_engine_item_event_requeues_when_jellyfin_item_is_unresolved(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/missing.mkv",
        paths=("/media/missing.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    requeued = []
    logger = FakeLogger()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
        requeue_callback=lambda event: requeued.append(event) or True,
    )
    engine._logger = logger

    result = await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.refreshed is True
    assert result.requeued_events == 1
    assert requeued[0].rating_key == 42
    assert requeued[0].requeue_count == 1
    requeue_log = next(kwargs for event, kwargs in logger.warning_calls if event == "sync.requeued")
    assert requeue_log["requeue_count"] == 1


async def test_sync_engine_userdata_sync_targets_merged_primary_only(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=True,
            content_hash="hash",
            last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="src-a",
                jellyfin_primary_id="item-1",
                is_primary=True,
            ),
            MediaSourceRecord(
                path="/media/b.mkv",
                plex_rating_key=42,
                jellyfin_source_id="src-b",
                jellyfin_primary_id="item-1",
                is_primary=False,
            ),
        ],
    )
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv"),
    )
    jellyfin = DistinctSourceIdMergedJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(
        SyncEvent(kind="userdata", source="webhook", rating_key=42, jellyfin_user_id="jf-user-1")
    )

    assert result.user_data_updated == 1
    assert jellyfin.user_data_reads == [("jf-user-1", "item-1")]
    assert jellyfin.played_calls == [("jf-user-1", "item-1")]
    assert [call[1] for call in jellyfin.updated_user_data] == ["item-1"]
    assert jellyfin.unplayed_calls == []


async def test_sync_engine_item_event_logs_error_when_requeue_is_exhausted(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/missing.mkv",
        paths=("/media/missing.mkv",),
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
        requeue_callback=lambda event: False,
    )

    result = await engine.handle_event(
        SyncEvent(kind="item", source="webhook", rating_key=42, requeue_count=config.sync.merging.max_requeue_count)
    )
    sync_log = state.get_sync_log(1)

    assert result.requeued_events == 0
    assert result.errors == 1
    assert sync_log is not None
    assert "could not be requeued" in json.loads(sync_log.error_detail)[0]


async def test_sync_engine_keeps_mapping_when_item_is_missing_in_plex_but_still_exists_in_jellyfin(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=False,
            content_hash="hash",
            last_synced_at=now,
        )
    )
    jellyfin = FakeJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(None, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert state.get_item_map(42) is not None


async def test_sync_engine_does_not_prune_mapping_when_plex_lookup_errors(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=False,
            content_hash="hash",
            last_synced_at=now,
        )
    )
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FailingLookupPlexClient(None, PlexUserData()),
        jellyfin=FakeJellyfinClient(),
    )

    with pytest.raises(RuntimeError, match="temporary plex lookup failure"):
        await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert state.get_item_map(42) is not None


async def test_sync_engine_prunes_mapping_when_item_is_missing_in_both_systems(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=False,
            content_hash="hash",
            last_synced_at=now,
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="item-1",
                jellyfin_primary_id="item-1",
                is_primary=True,
            )
        ],
    )
    jellyfin = FakeJellyfinClient()
    jellyfin.by_id.pop("item-1", None)
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(None, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert state.get_item_map(42) is None
    assert state.get_media_sources_for_rating_key(42) == []


async def test_sync_engine_writes_metadata_only_once_to_primary_for_merged_item(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv", "/media/c.mkv"),
        studio="Studio A",
    )
    jellyfin = FakeJellyfinClient()
    for item_id, path in [("item-2", "/media/b.mkv"), ("item-3", "/media/c.mkv")]:
        jellyfin.by_id[item_id] = JellyfinItem(item_id, path, DesiredMetadata())
        jellyfin.by_path[path] = jellyfin.by_id[item_id]
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(plex_item, PlexUserData()),
        jellyfin=jellyfin,
    )

    await engine.handle_event(SyncEvent(kind="item", source="webhook", rating_key=42))

    assert jellyfin.merge_calls == [("item-1", "item-2", "item-3")]
    assert jellyfin.updated_metadata == [
        (
            "item-1",
            DesiredMetadata(studios=("Studio A",), locked_fields=("Cast", "Studios")),
        )
    ]


async def test_sync_engine_writes_user_data_only_to_primary_for_merged_item(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=42,
            jellyfin_primary_id="item-1",
            is_merged=True,
            content_hash="hash",
            last_synced_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    state.replace_media_sources(
        42,
        [
            MediaSourceRecord(
                path="/media/a.mkv",
                plex_rating_key=42,
                jellyfin_source_id="src-a",
                jellyfin_primary_id="item-1",
                is_primary=True,
            ),
            MediaSourceRecord(
                path="/media/b.mkv",
                plex_rating_key=42,
                jellyfin_source_id="src-b",
                jellyfin_primary_id="item-1",
                is_primary=False,
            ),
        ],
    )
    plex_item = PlexItem(
        rating_key=42,
        path="/media/a.mkv",
        paths=("/media/a.mkv", "/media/b.mkv"),
    )
    jellyfin = DistinctSourceIdMergedJellyfinClient()
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FakePlexClient(
            plex_item,
            PlexUserData(
                watched=True,
                play_count=2,
                rating=8.0,
                last_viewed_at=datetime(2024, 1, 2, tzinfo=UTC),
            ),
        ),
        jellyfin=jellyfin,
    )

    result = await engine.handle_event(
        SyncEvent(kind="userdata", source="webhook", rating_key=42, jellyfin_user_id="jf-user-1")
    )

    assert result.user_data_updated == 1
    assert jellyfin.played_calls == [("jf-user-1", "item-1")]
    assert jellyfin.updated_user_data == [
        ("jf-user-1", "item-1", 2, 8.0, datetime(2024, 1, 2, tzinfo=UTC))
    ]
