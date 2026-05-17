from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from plex_jellyfin_sync.models import (
    CollectionMapRecord,
    ItemMapRecord,
    MediaSourceRecord,
    SyncLogRecord,
    UserDataMapRecord,
)


LATEST_SCHEMA_VERSION = 2

SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS item_map (
    plex_rating_key       INTEGER PRIMARY KEY,
    jellyfin_primary_id   TEXT NOT NULL,
    is_merged             INTEGER NOT NULL DEFAULT 0,
    content_hash          TEXT NOT NULL,
    last_synced_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_source_map (
    path                  TEXT PRIMARY KEY,
    plex_rating_key       INTEGER NOT NULL,
    jellyfin_source_id    TEXT NOT NULL,
    jellyfin_primary_id   TEXT NOT NULL,
    is_primary            INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (plex_rating_key) REFERENCES item_map(plex_rating_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_media_source_ratingkey ON media_source_map(plex_rating_key);
CREATE INDEX IF NOT EXISTS idx_media_source_jellyfin_id ON media_source_map(jellyfin_source_id);

CREATE TABLE IF NOT EXISTS collection_map (
    plex_collection_key INTEGER PRIMARY KEY,
    jellyfin_id         TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    last_synced_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_cache (
    name           TEXT PRIMARY KEY,
    jellyfin_id    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_data_map (
    plex_rating_key      INTEGER NOT NULL,
    jellyfin_user_id     TEXT NOT NULL,
    last_plex_viewcount  INTEGER NOT NULL DEFAULT 0,
    last_plex_watched    INTEGER NOT NULL DEFAULT 0,
    last_plex_rating     REAL,
    last_plex_lastviewed TEXT,
    last_synced_at       TEXT NOT NULL,
    PRIMARY KEY (plex_rating_key, jellyfin_user_id),
    FOREIGN KEY (plex_rating_key) REFERENCES item_map(plex_rating_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sync_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    trigger           TEXT NOT NULL,
    scope             TEXT NOT NULL,
    items_examined    INTEGER,
    items_updated     INTEGER,
    user_data_updated INTEGER,
    merges_applied    INTEGER,
    unmerges_applied  INTEGER,
    requeued_events   INTEGER,
    errors            INTEGER,
    error_detail      TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO schema_version (version) VALUES (2);
"""

MIGRATION_1_TO_2_SQL = """
ALTER TABLE media_source_map ADD COLUMN jellyfin_primary_id TEXT;
UPDATE media_source_map
SET jellyfin_primary_id = (
    SELECT item_map.jellyfin_primary_id
    FROM item_map
    WHERE item_map.plex_rating_key = media_source_map.plex_rating_key
)
WHERE jellyfin_primary_id IS NULL;
"""


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class StateStore:
    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.sqlite_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")

    def initialize(self) -> None:
        version = self._schema_version()
        if version == 0:
            self._initialize_fresh_schema()
            return
        self._migrate(version)

    def journal_mode(self) -> str:
        row = self._connection.execute("PRAGMA journal_mode").fetchone()
        if row is None:
            return ""
        if isinstance(row, sqlite3.Row):
            return str(row[0]).lower()
        return str(row[0]).lower()

    def ping(self) -> bool:
        try:
            self._connection.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def count_item_maps(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM item_map").fetchone()
        return int(row["count"]) if row is not None else 0

    def count_collection_maps(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM collection_map").fetchone()
        return int(row["count"]) if row is not None else 0

    def get_last_successful_full_sync_at(self) -> str | None:
        row = self._connection.execute(
            """
            SELECT completed_at
            FROM sync_log
            WHERE scope = 'full' AND completed_at IS NOT NULL AND errors = 0
            ORDER BY completed_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        value = row["completed_at"]
        return None if value is None else str(value)

    def close(self) -> None:
        self._connection.close()

    def get_item_map(self, plex_rating_key: int) -> ItemMapRecord | None:
        row = self._connection.execute(
            """
            SELECT plex_rating_key, jellyfin_primary_id, is_merged, content_hash, last_synced_at
            FROM item_map
            WHERE plex_rating_key = ?
            """,
            (plex_rating_key,),
        ).fetchone()
        return self._item_map_from_row(row)

    def list_item_maps(self) -> list[ItemMapRecord]:
        rows = self._connection.execute(
            """
            SELECT plex_rating_key, jellyfin_primary_id, is_merged, content_hash, last_synced_at
            FROM item_map
            ORDER BY plex_rating_key
            """
        ).fetchall()
        return [self._item_map_from_row(row) for row in rows if row is not None]

    def upsert_item_map(self, record: ItemMapRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO item_map (
                plex_rating_key, jellyfin_primary_id, is_merged, content_hash, last_synced_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plex_rating_key) DO UPDATE SET
                jellyfin_primary_id = excluded.jellyfin_primary_id,
                is_merged = excluded.is_merged,
                content_hash = excluded.content_hash,
                last_synced_at = excluded.last_synced_at
            """,
            (
                record.plex_rating_key,
                record.jellyfin_primary_id,
                int(record.is_merged),
                record.content_hash,
                _isoformat(record.last_synced_at),
            ),
        )
        self._connection.commit()

    def delete_item_map(self, plex_rating_key: int) -> None:
        self._connection.execute("DELETE FROM item_map WHERE plex_rating_key = ?", (plex_rating_key,))
        self._connection.commit()

    def replace_media_sources(self, plex_rating_key: int, records: list[MediaSourceRecord]) -> None:
        self._connection.execute("DELETE FROM media_source_map WHERE plex_rating_key = ?", (plex_rating_key,))
        self._connection.executemany(
            """
            INSERT INTO media_source_map (
                path, plex_rating_key, jellyfin_source_id, jellyfin_primary_id, is_primary
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    record.path,
                    record.plex_rating_key,
                    record.jellyfin_source_id,
                    record.jellyfin_primary_id,
                    int(record.is_primary),
                )
                for record in records
            ],
        )
        self._connection.commit()

    def get_media_sources_for_rating_key(self, plex_rating_key: int) -> list[MediaSourceRecord]:
        rows = self._connection.execute(
            """
            SELECT path, plex_rating_key, jellyfin_source_id, jellyfin_primary_id, is_primary
            FROM media_source_map
            WHERE plex_rating_key = ?
            ORDER BY is_primary DESC, path
            """,
            (plex_rating_key,),
        ).fetchall()
        return [self._media_source_from_row(row) for row in rows]

    def get_media_source_by_path(self, path: str) -> MediaSourceRecord | None:
        row = self._connection.execute(
            """
            SELECT path, plex_rating_key, jellyfin_source_id, jellyfin_primary_id, is_primary
            FROM media_source_map
            WHERE path = ?
            """,
            (path,),
        ).fetchone()
        return self._media_source_from_row(row)

    def get_primary_and_sources(self, plex_rating_key: int) -> tuple[str | None, list[MediaSourceRecord]]:
        item_map = self.get_item_map(plex_rating_key)
        return (
            item_map.jellyfin_primary_id if item_map is not None else None,
            self.get_media_sources_for_rating_key(plex_rating_key),
        )

    def upsert_collection_map(self, record: CollectionMapRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO collection_map (plex_collection_key, jellyfin_id, name, last_synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(plex_collection_key) DO UPDATE SET
                jellyfin_id = excluded.jellyfin_id,
                name = excluded.name,
                last_synced_at = excluded.last_synced_at
            """,
            (
                record.plex_collection_key,
                record.jellyfin_id,
                record.name,
                _isoformat(record.last_synced_at),
            ),
        )
        self._connection.commit()

    def list_collection_maps(self) -> list[CollectionMapRecord]:
        rows = self._connection.execute(
            """
            SELECT plex_collection_key, jellyfin_id, name, last_synced_at
            FROM collection_map
            ORDER BY plex_collection_key
            """
        ).fetchall()
        return [self._collection_map_from_row(row) for row in rows]

    def delete_collection_map(self, plex_collection_key: int) -> None:
        self._connection.execute(
            "DELETE FROM collection_map WHERE plex_collection_key = ?",
            (plex_collection_key,),
        )
        self._connection.commit()

    def get_person_cache(self, name: str) -> str | None:
        row = self._connection.execute(
            "SELECT jellyfin_id FROM person_cache WHERE name = ?",
            (name,),
        ).fetchone()
        return None if row is None else str(row["jellyfin_id"])

    def upsert_person_cache(self, *, name: str, jellyfin_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO person_cache (name, jellyfin_id)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET jellyfin_id = excluded.jellyfin_id
            """,
            (name, jellyfin_id),
        )
        self._connection.commit()

    def get_user_data_map(self, plex_rating_key: int, jellyfin_user_id: str) -> UserDataMapRecord | None:
        row = self._connection.execute(
            """
            SELECT plex_rating_key, jellyfin_user_id, last_plex_viewcount, last_plex_watched,
                   last_plex_rating, last_plex_lastviewed, last_synced_at
            FROM user_data_map
            WHERE plex_rating_key = ? AND jellyfin_user_id = ?
            """,
            (plex_rating_key, jellyfin_user_id),
        ).fetchone()
        return self._user_data_map_from_row(row)

    def upsert_user_data_map(self, record: UserDataMapRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO user_data_map (
                plex_rating_key, jellyfin_user_id, last_plex_viewcount, last_plex_watched,
                last_plex_rating, last_plex_lastviewed, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plex_rating_key, jellyfin_user_id) DO UPDATE SET
                last_plex_viewcount = excluded.last_plex_viewcount,
                last_plex_watched = excluded.last_plex_watched,
                last_plex_rating = excluded.last_plex_rating,
                last_plex_lastviewed = excluded.last_plex_lastviewed,
                last_synced_at = excluded.last_synced_at
            """,
            (
                record.plex_rating_key,
                record.jellyfin_user_id,
                record.last_plex_viewcount,
                int(record.last_plex_watched),
                record.last_plex_rating,
                _isoformat(record.last_plex_lastviewed),
                _isoformat(record.last_synced_at),
            ),
        )
        self._connection.commit()

    def create_sync_log(self, record: SyncLogRecord) -> int:
        cursor = self._connection.execute(
            """
            INSERT INTO sync_log (
                started_at, completed_at, trigger, scope, items_examined, items_updated,
                user_data_updated, merges_applied, unmerges_applied, requeued_events,
                errors, error_detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _isoformat(record.started_at),
                _isoformat(record.completed_at),
                record.trigger,
                record.scope,
                record.items_examined,
                record.items_updated,
                record.user_data_updated,
                record.merges_applied,
                record.unmerges_applied,
                record.requeued_events,
                record.errors,
                record.error_detail,
            ),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def update_sync_log(self, sync_log_id: int, record: SyncLogRecord) -> None:
        self._connection.execute(
            """
            UPDATE sync_log SET
                completed_at = ?,
                items_examined = ?,
                items_updated = ?,
                user_data_updated = ?,
                merges_applied = ?,
                unmerges_applied = ?,
                requeued_events = ?,
                errors = ?,
                error_detail = ?
            WHERE id = ?
            """,
            (
                _isoformat(record.completed_at),
                record.items_examined,
                record.items_updated,
                record.user_data_updated,
                record.merges_applied,
                record.unmerges_applied,
                record.requeued_events,
                record.errors,
                record.error_detail,
                sync_log_id,
            ),
        )
        self._connection.commit()

    def get_sync_log(self, sync_log_id: int) -> SyncLogRecord | None:
        row = self._connection.execute(
            """
            SELECT started_at, completed_at, trigger, scope, items_examined, items_updated,
                   user_data_updated, merges_applied, unmerges_applied, requeued_events,
                   errors, error_detail
            FROM sync_log
            WHERE id = ?
            """,
            (sync_log_id,),
        ).fetchone()
        return self._sync_log_from_row(row)

    def list_recent_sync_logs(self, limit: int = 50) -> list[SyncLogRecord]:
        rows = self._connection.execute(
            """
            SELECT started_at, completed_at, trigger, scope, items_examined, items_updated,
                   user_data_updated, merges_applied, unmerges_applied, requeued_events,
                   errors, error_detail
            FROM sync_log
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._sync_log_from_row(row) for row in rows if row is not None]

    def _item_map_from_row(self, row: sqlite3.Row | None) -> ItemMapRecord | None:
        if row is None:
            return None
        return ItemMapRecord(
            plex_rating_key=int(row["plex_rating_key"]),
            jellyfin_primary_id=str(row["jellyfin_primary_id"]),
            is_merged=bool(row["is_merged"]),
            content_hash=str(row["content_hash"]),
            last_synced_at=_parse_datetime(str(row["last_synced_at"])) or datetime.min,
        )

    def _media_source_from_row(self, row: sqlite3.Row | None) -> MediaSourceRecord | None:
        if row is None:
            return None
        return MediaSourceRecord(
            path=str(row["path"]),
            plex_rating_key=int(row["plex_rating_key"]),
            jellyfin_source_id=str(row["jellyfin_source_id"]),
            jellyfin_primary_id=str(row["jellyfin_primary_id"] or row["jellyfin_source_id"]),
            is_primary=bool(row["is_primary"]),
        )

    def _collection_map_from_row(self, row: sqlite3.Row | None) -> CollectionMapRecord | None:
        if row is None:
            return None
        return CollectionMapRecord(
            plex_collection_key=int(row["plex_collection_key"]),
            jellyfin_id=str(row["jellyfin_id"]),
            name=str(row["name"]),
            last_synced_at=_parse_datetime(str(row["last_synced_at"])) or datetime.min,
        )

    def _user_data_map_from_row(self, row: sqlite3.Row | None) -> UserDataMapRecord | None:
        if row is None:
            return None
        return UserDataMapRecord(
            plex_rating_key=int(row["plex_rating_key"]),
            jellyfin_user_id=str(row["jellyfin_user_id"]),
            last_plex_viewcount=int(row["last_plex_viewcount"]),
            last_plex_watched=bool(row["last_plex_watched"]),
            last_plex_rating=float(row["last_plex_rating"]) if row["last_plex_rating"] is not None else None,
            last_plex_lastviewed=_parse_datetime(row["last_plex_lastviewed"]),
            last_synced_at=_parse_datetime(str(row["last_synced_at"])) or datetime.min,
        )

    def _sync_log_from_row(self, row: sqlite3.Row | None) -> SyncLogRecord | None:
        if row is None:
            return None
        return SyncLogRecord(
            trigger=str(row["trigger"]),
            scope=str(row["scope"]),
            started_at=_parse_datetime(str(row["started_at"])) or datetime.min,
            completed_at=_parse_datetime(row["completed_at"]),
            items_examined=int(row["items_examined"] or 0),
            items_updated=int(row["items_updated"] or 0),
            user_data_updated=int(row["user_data_updated"] or 0),
            merges_applied=int(row["merges_applied"] or 0),
            unmerges_applied=int(row["unmerges_applied"] or 0),
            requeued_events=int(row["requeued_events"] or 0),
            errors=int(row["errors"] or 0),
            error_detail=str(row["error_detail"]) if row["error_detail"] is not None else None,
        )

    def _initialize_fresh_schema(self) -> None:
        with closing(self._connection.cursor()) as cursor:
            cursor.executescript(SCHEMA_V2_SQL)
        self._connection.commit()

    def _schema_version(self) -> int:
        row = self._connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_version'
            """
        ).fetchone()
        if row is None:
            media_source_columns = self._table_columns("media_source_map")
            if media_source_columns:
                return 1 if "jellyfin_primary_id" not in media_source_columns else LATEST_SCHEMA_VERSION
            return 0

        version_row = self._connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
        if version_row is None or version_row["version"] is None:
            return 0
        return int(version_row["version"])

    def _migrate(self, version: int) -> None:
        current_version = version
        if current_version < 2:
            self._migrate_1_to_2()
            current_version = 2
        if current_version > LATEST_SCHEMA_VERSION:
            raise sqlite3.DatabaseError(
                f"Database schema version {current_version} is newer than supported version {LATEST_SCHEMA_VERSION}"
            )
        self._connection.executescript(SCHEMA_V2_SQL)
        self._connection.execute("DELETE FROM schema_version")
        self._connection.execute("INSERT INTO schema_version (version) VALUES (?)", (LATEST_SCHEMA_VERSION,))
        self._connection.commit()

    def _migrate_1_to_2(self) -> None:
        columns = self._table_columns("media_source_map")
        if "jellyfin_primary_id" not in columns:
            self._connection.executescript(MIGRATION_1_TO_2_SQL)
        self._connection.commit()

    def _table_columns(self, table_name: str) -> set[str]:
        rows = self._connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}
