from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

import pytest

from plex_jellyfin_sync.models import (
    CollectionMapRecord,
    ItemMapRecord,
    MediaSourceRecord,
    SyncLogRecord,
    UserDataMapRecord,
)
from plex_jellyfin_sync.state import StateStore


def test_state_store_round_trips_item_and_media_source_records(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)

    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=1,
            jellyfin_primary_id="jf-1",
            is_merged=True,
            content_hash="hash-1",
            last_synced_at=now,
        )
    )
    state.replace_media_sources(
        1,
        [
            MediaSourceRecord("/a.mkv", 1, "src-a", "jf-1", True),
            MediaSourceRecord("/b.mkv", 1, "src-b", "jf-1", False),
        ],
    )

    item = state.get_item_map(1)
    sources = state.get_media_sources_for_rating_key(1)

    assert item is not None
    assert item.jellyfin_primary_id == "jf-1"
    assert item.is_merged is True
    assert [source.path for source in sources] == ["/a.mkv", "/b.mkv"]
    assert state.get_media_source_by_path("/b.mkv").jellyfin_source_id == "src-b"


def test_state_store_round_trips_collection_person_user_data_and_sync_logs(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)

    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=1,
            jellyfin_primary_id="jf-1",
            is_merged=False,
            content_hash="hash-1",
            last_synced_at=now,
        )
    )
    state.upsert_collection_map(CollectionMapRecord(10, "box-1", "Favorites", now))
    state.upsert_person_cache(name="Alice", jellyfin_id="person-1")
    state.upsert_user_data_map(
        UserDataMapRecord(
            plex_rating_key=1,
            jellyfin_user_id="user-1",
            last_plex_viewcount=3,
            last_plex_watched=True,
            last_plex_rating=8.0,
            last_plex_lastviewed=now,
            last_synced_at=now,
        )
    )
    log_id = state.create_sync_log(SyncLogRecord(trigger="manual", scope="full", started_at=now))
    state.update_sync_log(
        log_id,
        SyncLogRecord(
            trigger="manual",
            scope="full",
            started_at=now,
            completed_at=now,
            items_examined=5,
            items_updated=2,
            user_data_updated=1,
        ),
    )

    collections = state.list_collection_maps()
    user_data = state.get_user_data_map(1, "user-1")
    sync_log = state.get_sync_log(log_id)

    assert collections[0].name == "Favorites"
    assert state.get_person_cache("Alice") == "person-1"
    assert user_data is not None
    assert user_data.last_plex_viewcount == 3
    assert sync_log is not None
    assert sync_log.items_examined == 5
    assert sync_log.completed_at == now


def test_state_store_collection_map_crud_round_trip(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)

    state.upsert_collection_map(CollectionMapRecord(10, "box-1", "Favorites", now))

    assert state.list_collection_maps() == [CollectionMapRecord(10, "box-1", "Favorites", now)]

    state.delete_collection_map(10)

    assert state.list_collection_maps() == []


def test_state_store_person_cache_reports_miss_then_hit(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()

    assert state.get_person_cache("Alice") is None

    state.upsert_person_cache(name="Alice", jellyfin_id="person-1")

    assert state.get_person_cache("Alice") == "person-1"


def test_state_store_user_data_map_supports_multiple_users_per_item(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=1,
            jellyfin_primary_id="jf-1",
            is_merged=False,
            content_hash="hash-1",
            last_synced_at=now,
        )
    )

    state.upsert_user_data_map(UserDataMapRecord(1, "user-1", 1, True, 8.0, now, now))
    state.upsert_user_data_map(UserDataMapRecord(1, "user-2", 2, True, 9.0, now, now))

    assert state.get_user_data_map(1, "user-1").last_plex_viewcount == 1
    assert state.get_user_data_map(1, "user-2").last_plex_viewcount == 2


def test_state_store_delete_item_map_cascades_media_sources_and_user_data(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=1,
            jellyfin_primary_id="jf-1",
            is_merged=True,
            content_hash="hash-1",
            last_synced_at=now,
        )
    )
    state.replace_media_sources(1, [MediaSourceRecord("/a.mkv", 1, "src-a", "jf-1", True)])
    state.upsert_user_data_map(UserDataMapRecord(1, "user-1", 1, True, 8.0, now, now))

    state.delete_item_map(1)

    assert state.get_item_map(1) is None
    assert state.get_media_sources_for_rating_key(1) == []
    assert state.get_user_data_map(1, "user-1") is None


def test_state_store_collection_map_unique_constraint_raises_integrity_error(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_collection_map(CollectionMapRecord(10, "box-1", "Favorites", now))

    with pytest.raises(sqlite3.IntegrityError):
        state.upsert_collection_map(CollectionMapRecord(11, "box-1", "Favorites 2", now))


def test_state_store_enables_wal_mode(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()

    assert state.journal_mode() == "wal"


def test_state_store_get_primary_and_sources_returns_merged_source_set(tmp_path) -> None:
    state = StateStore(str(tmp_path / "sync.db"))
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=7,
            jellyfin_primary_id="jf-primary",
            is_merged=True,
            content_hash="hash-7",
            last_synced_at=now,
        )
    )
    state.replace_media_sources(
        7,
        [
            MediaSourceRecord("/a.mkv", 7, "src-a", "jf-primary", True),
            MediaSourceRecord("/b.mkv", 7, "src-b", "jf-primary", False),
        ],
    )

    primary_id, sources = state.get_primary_and_sources(7)

    assert primary_id == "jf-primary"
    assert [source.path for source in sources] == ["/a.mkv", "/b.mkv"]


def test_state_store_migrates_legacy_v1_schema_to_latest(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE item_map (
            plex_rating_key INTEGER PRIMARY KEY,
            jellyfin_primary_id TEXT NOT NULL,
            is_merged INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL,
            last_synced_at TEXT NOT NULL
        );

        CREATE TABLE media_source_map (
            path TEXT PRIMARY KEY,
            plex_rating_key INTEGER NOT NULL,
            jellyfin_source_id TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (plex_rating_key) REFERENCES item_map(plex_rating_key) ON DELETE CASCADE
        );

        INSERT INTO item_map (
            plex_rating_key, jellyfin_primary_id, is_merged, content_hash, last_synced_at
        ) VALUES (1, 'jf-1', 1, 'hash-1', '2024-01-01T00:00:00+00:00');

        INSERT INTO media_source_map (
            path, plex_rating_key, jellyfin_source_id, is_primary
        ) VALUES ('/a.mkv', 1, 'src-a', 1);
        """
    )
    connection.commit()
    connection.close()

    state = StateStore(str(db_path))
    state.initialize()

    source = state.get_media_source_by_path("/a.mkv")
    primary_id, sources = state.get_primary_and_sources(1)

    assert source is not None
    assert source.jellyfin_primary_id == "jf-1"
    assert primary_id == "jf-1"
    assert sources[0].jellyfin_primary_id == "jf-1"
