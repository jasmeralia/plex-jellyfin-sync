from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import time

import structlog

from plex_jellyfin_sync.config import AppConfig, UserMappingEntry
from plex_jellyfin_sync.diff import compute_content_hash, diff_metadata
from plex_jellyfin_sync.jellyfin_client import JellyfinClient
from plex_jellyfin_sync.mapper import build_desired_metadata
from plex_jellyfin_sync.merge_planner import plan_merge
from plex_jellyfin_sync.models import (
    CollectionMapRecord,
    ItemMapRecord,
    MediaSourceRecord,
    MergePlan,
    PersonRef,
    PlexItem,
    SyncEvent,
    SyncLogRecord,
    UserDataMapRecord,
    utc_now,
)
from plex_jellyfin_sync.path_mapper import PathMapper
from plex_jellyfin_sync.plex_client import PlexClient
from plex_jellyfin_sync.state import StateStore
from plex_jellyfin_sync.user_data_merger import merge_user_data


@dataclass(frozen=True)
class SyncResult:
    scope: str
    started_at: datetime
    completed_at: datetime
    duration_ms: int = 0
    items_examined: int = 0
    items_updated: int = 0
    user_data_updated: int = 0
    merges_applied: int = 0
    unmerges_applied: int = 0
    requeued_events: int = 0
    errors: int = 0


class UnresolvedPathsError(RuntimeError):
    def __init__(self, paths: list[str]) -> None:
        super().__init__(f"Unresolved Jellyfin item paths: {', '.join(paths)}")
        self.paths = tuple(paths)


class SyncEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        state: StateStore,
        plex: PlexClient,
        jellyfin: JellyfinClient,
        path_mapper: PathMapper | None = None,
        requeue_callback: Callable[[SyncEvent], bool] | None = None,
        sleep_func: Callable[[float], None] | None = None,
        monotonic_func: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._plex = plex
        self._jellyfin = jellyfin
        self._path_mapper = path_mapper or PathMapper(config.path_mapping.rules)
        self._requeue_callback = requeue_callback
        self._sleep = sleep_func or time.sleep
        self._monotonic = monotonic_func or time.monotonic
        self._logger = structlog.get_logger(__name__)

    async def handle_event(self, event: SyncEvent) -> SyncResult:
        started_at = utc_now()
        scope = self._scope_for_event(event)
        sync_log_id = self._state.create_sync_log(
            SyncLogRecord(trigger=event.source, scope=scope, started_at=started_at)
        )
        self._logger.info("sync.started", scope=scope, source=event.source)

        stats = {
            "items_examined": 0,
            "items_updated": 0,
            "user_data_updated": 0,
            "merges_applied": 0,
            "unmerges_applied": 0,
            "requeued_events": 0,
            "errors": 0,
            "error_details": [],
        }

        try:
            if event.kind == "full":
                self._handle_full_sync(stats)
            elif event.kind == "userdata":
                self._handle_user_data_event(event, stats)
            else:
                self._handle_item_event(event, stats)
        except Exception as exc:
            self._append_error(stats, str(exc))
            raise
        finally:
            completed_at = utc_now()
            duration_ms = max(int((completed_at - started_at).total_seconds() * 1000), 0)
            self._state.update_sync_log(
                sync_log_id,
                SyncLogRecord(
                    trigger=event.source,
                    scope=scope,
                    started_at=started_at,
                    completed_at=completed_at,
                    items_examined=int(stats["items_examined"]),
                    items_updated=int(stats["items_updated"]),
                    user_data_updated=int(stats["user_data_updated"]),
                    merges_applied=int(stats["merges_applied"]),
                    unmerges_applied=int(stats["unmerges_applied"]),
                    requeued_events=int(stats["requeued_events"]),
                    errors=int(stats["errors"]),
                    error_detail=self._encode_error_details(stats),
                ),
            )
            self._logger.info("sync.completed", scope=scope, source=event.source, duration_ms=duration_ms, **stats)

        return SyncResult(
            scope=scope,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            items_examined=int(stats["items_examined"]),
            items_updated=int(stats["items_updated"]),
            user_data_updated=int(stats["user_data_updated"]),
            merges_applied=int(stats["merges_applied"]),
            unmerges_applied=int(stats["unmerges_applied"]),
            requeued_events=int(stats["requeued_events"]),
            errors=int(stats["errors"]),
        )

    def _handle_full_sync(self, stats: dict[str, int | str | None]) -> None:
        plex_items = self._plex.list_items()
        self._jellyfin.trigger_library_refresh()
        self._wait_for_library_refresh(expected_count=len(plex_items), stats=stats)
        seen_rating_keys: set[int] = set()
        for item in plex_items:
            seen_rating_keys.add(item.rating_key)
            stats["items_examined"] = int(stats["items_examined"]) + 1
            try:
                updated, merge_action = self._sync_item(item)
            except UnresolvedPathsError as exc:
                self._append_error(stats, f"ratingKey {item.rating_key}: unresolved paths after refresh wait: {', '.join(exc.paths)}")
                self._logger.warning("sync.item_unresolved", rating_key=item.rating_key, paths=list(exc.paths))
            else:
                self._record_merge_stats(stats, merge_action)
                if updated:
                    stats["items_updated"] = int(stats["items_updated"]) + 1
            for mapping in self._config.user_mapping:
                if self._sync_user_data_safely(item, mapping, current_event=None, stats=stats):
                    stats["user_data_updated"] = int(stats["user_data_updated"]) + 1
        if self._config.sync.field_mapping.collections:
            self._sync_collections()
        self._prune_deleted_items(seen_rating_keys)

    def _handle_item_event(self, event: SyncEvent, stats: dict[str, int | str | None]) -> None:
        if event.rating_key is None:
            raise ValueError("Item sync requires rating_key")
        item = self._plex.get_item(event.rating_key)
        if item is None:
            self._prune_deleted_item(event.rating_key)
            return
        stats["items_examined"] = int(stats["items_examined"]) + 1
        try:
            updated, merge_action = self._sync_item(item)
        except UnresolvedPathsError:
            if self._requeue_event(event):
                stats["requeued_events"] = int(stats["requeued_events"]) + 1
            else:
                self._append_error(stats, f"ratingKey {event.rating_key}: unresolved item could not be requeued")
            return
        else:
            self._record_merge_stats(stats, merge_action)
            if updated:
                stats["items_updated"] = int(stats["items_updated"]) + 1
        if self._config.sync.field_mapping.collections:
            self._sync_collections()
        for mapping in self._config.user_mapping:
            if self._sync_user_data_safely(item, mapping, current_event=event, stats=stats):
                stats["user_data_updated"] = int(stats["user_data_updated"]) + 1

    def _handle_user_data_event(self, event: SyncEvent, stats: dict[str, int | str | None]) -> None:
        if event.rating_key is None or event.jellyfin_user_id is None:
            raise ValueError("User-data sync requires rating_key and jellyfin_user_id")
        item = self._plex.get_item(event.rating_key)
        if item is None:
            return
        mapping = next(
            (
                entry
                for entry in self._config.user_mapping
                if entry.jellyfin_user_id == event.jellyfin_user_id
            ),
            None,
        )
        if mapping is None:
            return
        if self._sync_user_data_safely(item, mapping, current_event=event, stats=stats):
            stats["user_data_updated"] = int(stats["user_data_updated"]) + 1

    def _sync_item(self, item: PlexItem) -> tuple[bool, str]:
        field_mapping = self._config.sync.field_mapping
        merging_enabled = self._config.sync.merging.enabled
        mapped_paths = tuple(self._path_mapper.map_plex_to_jellyfin(path) for path in item.paths)
        desired_primary_path = self._path_mapper.map_plex_to_jellyfin(item.primary_path or item.path)
        tracked_paths = mapped_paths if merging_enabled else (desired_primary_path,)
        self._release_stale_path_claims(item.rating_key, tracked_paths)
        desired = build_desired_metadata(
            studio=item.studio if field_mapping.studio else None,
            writers=list(item.writers) if field_mapping.writers_as_actors else [],
            directors=list(item.directors) if field_mapping.directors else [],
            collections=list(item.collections) if field_mapping.collections else [],
            lock_synced_fields=self._config.sync.lock_synced_fields,
        )
        content_hash = compute_content_hash(desired)
        current_map = self._state.get_item_map(item.rating_key)
        current_source_records = self._state.get_media_sources_for_rating_key(item.rating_key)
        current_primary_id = current_map.jellyfin_primary_id if current_map is not None else None
        current_primary_item = self._jellyfin.get_item_or_none(current_primary_id) if current_primary_id is not None else None
        current_primary_sources = self._media_sources_for_item(current_primary_item)
        current_path_to_source_id = current_primary_sources or {
            record.path: record.jellyfin_source_id for record in current_source_records
        }
        resolved_item_ids, resolved_source_ids, unresolved = self._resolve_tracked_paths(
            tracked_paths=tracked_paths,
            desired_primary_path=desired_primary_path,
            current_source_records=current_source_records,
            current_primary_item=current_primary_item,
            current_primary_sources=current_primary_sources,
        )
        merge = plan_merge(
            desired_paths=tracked_paths,
            primary_path=desired_primary_path,
            desired_path_to_item_id={path: resolved_item_ids.get(path) for path in tracked_paths},
            current_path_to_item_id=current_path_to_source_id,
            current_primary_id=current_primary_id,
            current_primary_path=current_primary_item.path if current_primary_item is not None else None,
            previously_merged=current_map.is_merged if current_map is not None else False,
        )
        if merge.action == "defer":
            raise UnresolvedPathsError(list(merge.unresolved_paths))

        if merge.action == "unmerge":
            if current_primary_id is None:
                raise UnresolvedPathsError([desired_primary_path])
            self._jellyfin.unmerge_versions(current_primary_id)
            current_primary_item = None
            current_primary_sources = {}
            resolved_item_ids, resolved_source_ids, unresolved = self._resolve_tracked_paths(
                tracked_paths=(desired_primary_path,),
                desired_primary_path=desired_primary_path,
                current_source_records=current_source_records,
                current_primary_item=None,
                current_primary_sources={},
            )
            if unresolved:
                raise UnresolvedPathsError(unresolved)
            primary_item_id = resolved_item_ids.get(desired_primary_path)
            if primary_item_id is None:
                raise UnresolvedPathsError([desired_primary_path])
            merge = MergePlan(action="unmerge", primary_id=primary_item_id)
        elif merge.action in {"merge", "rebuild"}:
            if merge.action == "rebuild":
                self._logger.warning(
                    "sync.remerge_overwrite",
                    plex_rating_key=item.rating_key,
                    current_primary_id=current_primary_id,
                    target_primary_id=merge.primary_id,
                    paths=list(mapped_paths),
                )
                if current_primary_id is None:
                    raise UnresolvedPathsError(list(mapped_paths))
                self._jellyfin.unmerge_versions(current_primary_id)
                current_primary_item = None
                current_primary_sources = {}
                resolved_item_ids, resolved_source_ids, unresolved = self._resolve_tracked_paths(
                    tracked_paths=mapped_paths,
                    desired_primary_path=desired_primary_path,
                    current_source_records=current_source_records,
                    current_primary_item=None,
                    current_primary_sources={},
                )
                if unresolved:
                    raise UnresolvedPathsError(unresolved)
            missing_item_ids = [path for path in tracked_paths if path not in resolved_item_ids]
            if missing_item_ids:
                raise UnresolvedPathsError(missing_item_ids)
            ordered_ids = tuple(resolved_item_ids[path] for path in tracked_paths)
            self._jellyfin.merge_versions(ordered_ids)
            merge = MergePlan(action=merge.action, primary_id=resolved_item_ids[desired_primary_path], ordered_ids=ordered_ids)

        primary_id = merge.primary_id
        if primary_id is None:
            raise UnresolvedPathsError([desired_primary_path])
        current_item = self._jellyfin.get_item(primary_id)
        current_item_sources = self._media_sources_for_item(current_item)
        metadata_diff = diff_metadata(current_item.metadata, desired)
        state_paths_match = {record.path for record in current_source_records} == set(tracked_paths)
        state_primary_matches = current_map is not None and current_map.jellyfin_primary_id == primary_id
        if (
            current_map is not None
            and current_map.content_hash == content_hash
            and merge.action == "noop"
            and state_paths_match
            and state_primary_matches
            and not metadata_diff.update_item
        ):
            return False, "noop"
        if metadata_diff.update_item:
            self._cache_known_people(desired.people)
            self._jellyfin.update_item_metadata(primary_id, desired)
            self._refresh_people_cache(desired.people)

        timestamp = utc_now()
        self._state.upsert_item_map(
            ItemMapRecord(
                plex_rating_key=item.rating_key,
                jellyfin_primary_id=primary_id,
                is_merged=len(tracked_paths) > 1,
                content_hash=content_hash,
                last_synced_at=timestamp,
            )
        )
        self._state.replace_media_sources(
            item.rating_key,
            [
                MediaSourceRecord(
                    path=path,
                    plex_rating_key=item.rating_key,
                    jellyfin_source_id=current_item_sources.get(path, resolved_source_ids.get(path, primary_id)),
                    jellyfin_primary_id=primary_id,
                    is_primary=(path == desired_primary_path),
                )
                for path in tracked_paths
            ],
        )
        return True, merge.action

    def _cache_known_people(self, people: tuple[PersonRef, ...]) -> None:
        for name in self._unique_person_names(people):
            if self._state.get_person_cache(name) is not None:
                continue
            person_id = self._jellyfin.find_person_id_by_name(name)
            if person_id is not None:
                self._state.upsert_person_cache(name=name, jellyfin_id=person_id)

    def _refresh_people_cache(self, people: tuple[PersonRef, ...]) -> None:
        for name in self._unique_person_names(people):
            person_id = self._jellyfin.find_person_id_by_name(name)
            if person_id is not None:
                self._state.upsert_person_cache(name=name, jellyfin_id=person_id)

    @staticmethod
    def _unique_person_names(people: tuple[PersonRef, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered_names: list[str] = []
        for person in people:
            if person.name in seen:
                continue
            seen.add(person.name)
            ordered_names.append(person.name)
        return tuple(ordered_names)

    @staticmethod
    def _media_sources_for_item(item) -> dict[str, str]:
        if item is None:
            return {}
        sources = {path: source_id for path, source_id in item.media_sources if path and source_id}
        if not sources and item.path:
            sources[item.path] = item.item_id
        return sources

    def _resolve_tracked_paths(
        self,
        *,
        tracked_paths: tuple[str, ...],
        desired_primary_path: str,
        current_source_records: list[MediaSourceRecord],
        current_primary_item,
        current_primary_sources: dict[str, str],
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        resolved_item_ids: dict[str, str] = {}
        resolved_source_ids: dict[str, str] = {}
        unresolved: list[str] = []

        for path in tracked_paths:
            if path in current_primary_sources:
                resolved_source_ids[path] = current_primary_sources[path]
                if current_primary_item is not None and path == desired_primary_path:
                    resolved_item_ids[path] = current_primary_item.item_id
                continue

            current_source_record = next((record for record in current_source_records if record.path == path), None)
            jellyfin_item = None
            if current_source_record is not None:
                jellyfin_item = self._jellyfin.get_item_or_none(current_source_record.jellyfin_source_id)
            if jellyfin_item is None:
                jellyfin_item = self._jellyfin.find_item_by_path(path)
            if jellyfin_item is None:
                unresolved.append(path)
                continue

            resolved_item_ids[path] = jellyfin_item.item_id
            source_ids = self._media_sources_for_item(jellyfin_item)
            resolved_source_ids[path] = source_ids.get(path, jellyfin_item.item_id)

        return resolved_item_ids, resolved_source_ids, unresolved

    def _release_stale_path_claims(self, rating_key: int, mapped_paths: tuple[str, ...]) -> None:
        conflicting_rating_keys = {
            record.plex_rating_key
            for path in mapped_paths
            if (record := self._state.get_media_source_by_path(path)) is not None and record.plex_rating_key != rating_key
        }
        for conflicting_rating_key in sorted(conflicting_rating_keys):
            conflict_map = self._state.get_item_map(conflicting_rating_key)
            if conflict_map is None:
                continue
            conflict_sources = self._state.get_media_sources_for_rating_key(conflicting_rating_key)
            if conflict_map.is_merged or len(conflict_sources) > 1:
                self._jellyfin.unmerge_versions(conflict_map.jellyfin_primary_id)
            self._state.delete_item_map(conflicting_rating_key)
            self._logger.warning(
                "sync.path_reassigned",
                previous_rating_key=conflicting_rating_key,
                current_rating_key=rating_key,
                paths=list(mapped_paths),
            )

    def _sync_user_data(self, item: PlexItem, mapping: UserMappingEntry, *, current_event: SyncEvent | None) -> bool:
        if not any(
            [
                self._config.sync.user_data.watched,
                self._config.sync.user_data.play_count,
                self._config.sync.user_data.rating,
            ]
        ):
            return False

        item_map = self._state.get_item_map(item.rating_key)
        if item_map is None:
            try:
                self._sync_item(item)
            except UnresolvedPathsError:
                if current_event is not None:
                    self._requeue_event(current_event)
                return False
            item_map = self._state.get_item_map(item.rating_key)
        if item_map is None:
            return False

        plex_user_data = self._plex.get_user_data(item.rating_key, token=mapping.plex_token)
        if plex_user_data is None:
            return False

        jellyfin_user_data = self._jellyfin.get_user_data(mapping.jellyfin_user_id, item_map.jellyfin_primary_id)
        plan = merge_user_data(plex_user_data, jellyfin_user_data)
        effective_mark_played = plan.mark_played if self._config.sync.user_data.watched else False
        effective_play_count = plan.play_count if self._config.sync.user_data.play_count else None
        effective_rating = plan.rating if self._config.sync.user_data.rating else None
        effective_last_played = (
            plan.last_played_date
            if self._config.sync.user_data.watched or self._config.sync.user_data.play_count
            else None
        )

        if not any(
            [
                effective_mark_played,
                effective_play_count is not None,
                effective_rating is not None,
                effective_last_played is not None,
            ]
        ):
            return False

        if effective_mark_played:
            self._jellyfin.mark_played(mapping.jellyfin_user_id, item_map.jellyfin_primary_id)
        self._jellyfin.update_user_data(
            mapping.jellyfin_user_id,
            item_map.jellyfin_primary_id,
            play_count=effective_play_count,
            rating=effective_rating,
            last_played_date=effective_last_played,
        )
        self._state.upsert_user_data_map(
            UserDataMapRecord(
                plex_rating_key=item.rating_key,
                jellyfin_user_id=mapping.jellyfin_user_id,
                last_plex_viewcount=plex_user_data.play_count,
                last_plex_watched=plex_user_data.watched,
                last_plex_rating=plex_user_data.rating,
                last_plex_lastviewed=plex_user_data.last_viewed_at,
                last_synced_at=utc_now(),
            )
        )
        self._logger.info(
            "sync.userdata_updated",
            plex_rating_key=item.rating_key,
            jellyfin_user_id=mapping.jellyfin_user_id,
            plex_account=mapping.plex_account,
            userdata_changes=self._userdata_changes(
                mark_played=effective_mark_played,
                play_count=effective_play_count,
                rating=effective_rating,
                last_played_date=effective_last_played,
            ),
            scope=self._scope_for_event(current_event) if current_event is not None else f"item:{item.rating_key}",
        )
        return True

    def _sync_user_data_safely(
        self,
        item: PlexItem,
        mapping: UserMappingEntry,
        *,
        current_event: SyncEvent | None,
        stats: dict[str, int | str | None],
    ) -> bool:
        try:
            return self._sync_user_data(item, mapping, current_event=current_event)
        except Exception as exc:
            self._append_error(
                stats,
                f"ratingKey {item.rating_key}, jellyfin_user_id {mapping.jellyfin_user_id}: user-data sync failed: {exc}",
            )
            self._logger.exception(
                "sync.userdata_failed",
                plex_rating_key=item.rating_key,
                jellyfin_user_id=mapping.jellyfin_user_id,
                plex_account=mapping.plex_account,
                scope=self._scope_for_event(current_event) if current_event is not None else f"item:{item.rating_key}",
            )
            return False

    def _scope_for_event(self, event: SyncEvent) -> str:
        if event.kind == "full":
            return "full"
        if event.kind == "userdata":
            return f"userdata:{event.rating_key}:{event.jellyfin_user_id}"
        return f"item:{event.rating_key}"

    def _record_merge_stats(self, stats: dict[str, int | str | None], merge_action: str) -> None:
        if merge_action in {"merge", "rebuild"}:
            stats["merges_applied"] = int(stats["merges_applied"]) + 1
        if merge_action in {"unmerge", "rebuild"}:
            stats["unmerges_applied"] = int(stats["unmerges_applied"]) + 1

    @staticmethod
    def _userdata_changes(
        *,
        mark_played: bool,
        play_count: int | None,
        rating: float | None,
        last_played_date: datetime | None,
    ) -> list[str]:
        changes: list[str] = []
        if mark_played:
            changes.append("watched")
        if play_count is not None:
            changes.append("play_count")
        if rating is not None:
            changes.append("rating")
        if last_played_date is not None:
            changes.append("last_played")
        return changes

    def _sync_collections(self) -> None:
        plex_collections = self._plex.list_collections()
        jellyfin_collections = {collection.name: collection for collection in self._jellyfin.list_collections()}
        existing_by_key = {record.plex_collection_key: record for record in self._state.list_collection_maps()}
        seen_collection_keys: set[int] = set()

        for plex_collection in plex_collections:
            seen_collection_keys.add(plex_collection.key)
            unresolved_member_keys = tuple(
                rating_key
                for rating_key in plex_collection.member_rating_keys
                if self._state.get_item_map(rating_key) is None
            )
            if unresolved_member_keys:
                continue
            desired_member_ids = tuple(
                item_map.jellyfin_primary_id
                for rating_key in plex_collection.member_rating_keys
                if (item_map := self._state.get_item_map(rating_key)) is not None
            )
            existing_map = existing_by_key.get(plex_collection.key)
            existing_collection = None
            if existing_map is not None:
                existing_collection = next(
                    (collection for collection in jellyfin_collections.values() if collection.collection_id == existing_map.jellyfin_id),
                    None,
                )
            if existing_collection is None:
                existing_collection = jellyfin_collections.get(plex_collection.name)

            if not desired_member_ids:
                if existing_collection is not None:
                    self._jellyfin.delete_item(existing_collection.collection_id)
                if existing_map is not None:
                    self._state.delete_collection_map(plex_collection.key)
                continue

            if existing_collection is None:
                collection_id = self._jellyfin.create_collection(plex_collection.name, desired_member_ids)
                self._state.upsert_collection_map(
                    CollectionMapRecord(
                        plex_collection_key=plex_collection.key,
                        jellyfin_id=collection_id,
                        name=plex_collection.name,
                        last_synced_at=utc_now(),
                    )
                )
                continue

            if existing_collection.name != plex_collection.name:
                self._jellyfin.rename_collection(existing_collection.collection_id, plex_collection.name)

            current_members = set(existing_collection.item_ids)
            desired_members = set(desired_member_ids)
            to_add = sorted(desired_members - current_members)
            to_remove = sorted(current_members - desired_members)
            if to_add:
                self._jellyfin.add_items_to_collection(existing_collection.collection_id, to_add)
            if to_remove:
                self._jellyfin.remove_items_from_collection(existing_collection.collection_id, to_remove)
            self._state.upsert_collection_map(
                CollectionMapRecord(
                    plex_collection_key=plex_collection.key,
                    jellyfin_id=existing_collection.collection_id,
                    name=plex_collection.name,
                    last_synced_at=utc_now(),
                )
            )

        for stale_record in self._state.list_collection_maps():
            if stale_record.plex_collection_key in seen_collection_keys:
                continue
            self._jellyfin.delete_item(stale_record.jellyfin_id)
            self._state.delete_collection_map(stale_record.plex_collection_key)

    def _prune_deleted_items(self, seen_rating_keys: set[int]) -> None:
        for record in self._state.list_item_maps():
            if record.plex_rating_key not in seen_rating_keys:
                self._prune_deleted_item(record.plex_rating_key)

    def _prune_deleted_item(self, rating_key: int) -> None:
        record = self._state.get_item_map(rating_key)
        if record is None:
            return
        if self._jellyfin.get_item_or_none(record.jellyfin_primary_id) is None:
            self._state.delete_item_map(rating_key)

    def _requeue_event(self, event: SyncEvent) -> bool:
        self._jellyfin.trigger_library_refresh()
        if self._requeue_callback is None:
            return False
        requeued_event = SyncEvent(
            kind=event.kind,
            source=event.source,
            rating_key=event.rating_key,
            plex_account=event.plex_account,
            jellyfin_user_id=event.jellyfin_user_id,
            requeue_count=event.requeue_count + 1,
            job_id=event.job_id,
            metadata=event.metadata,
        )
        queued = self._requeue_callback(requeued_event)
        if queued:
            self._logger.warning(
                "sync.requeued",
                kind=event.kind,
                plex_rating_key=event.rating_key,
                jellyfin_user_id=event.jellyfin_user_id,
                requeue_count=requeued_event.requeue_count,
            )
        return queued

    def _wait_for_library_refresh(self, *, expected_count: int, stats: dict[str, int | str | None]) -> None:
        timeout_seconds = self._config.sync.merging.refresh_timeout_seconds
        deadline = self._monotonic() + max(timeout_seconds, 0)
        highest_seen = 0
        while True:
            current_count = len(self._jellyfin.list_library_items())
            highest_seen = max(highest_seen, current_count)
            if current_count >= expected_count:
                return
            if self._monotonic() >= deadline:
                self._append_error(
                    stats,
                    f"library refresh timed out after {timeout_seconds}s; expected at least {expected_count} items, saw {highest_seen}",
                )
                return
            self._sleep(1.0)

    def _append_error(self, stats: dict[str, int | str | None], message: str) -> None:
        stats["errors"] = int(stats["errors"]) + 1
        error_details = stats.get("error_details")
        if isinstance(error_details, list):
            error_details.append(message)

    def _encode_error_details(self, stats: dict[str, int | str | None]) -> str | None:
        error_details = stats.get("error_details")
        if isinstance(error_details, list) and error_details:
            return json.dumps(error_details)
        return None
