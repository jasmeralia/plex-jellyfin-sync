from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import threading
import time

import httpx
import pytest

from plex_jellyfin_sync.config import AppConfig
from plex_jellyfin_sync.debounce_queue import DebounceQueue
from plex_jellyfin_sync.jellyfin_client import JellyfinClient, JellyfinClientError
from plex_jellyfin_sync.models import DesiredMetadata, PlexCollection, PlexItem, PlexUserData, SyncEvent
from plex_jellyfin_sync.path_mapper import PathMapper
from plex_jellyfin_sync.state import StateStore
from plex_jellyfin_sync.sync_engine import SyncEngine
from plex_jellyfin_sync.webhook_server import JobTracker, create_app


MEDIA_ROOT = "/media/othervideo"
UNSET = object()


class FunctionalPlexClient:
    def __init__(
        self,
        items: list[PlexItem],
        *,
        collections: list[PlexCollection] | None = None,
        user_data_by_rating_key: dict[int, PlexUserData] | None = None,
    ) -> None:
        self._items = {item.rating_key: item for item in items}
        self._collections = collections or []
        self._user_data_by_rating_key = user_data_by_rating_key or {}

    def list_items(self) -> list[PlexItem]:
        return list(self._items.values())

    def get_item(self, rating_key: int) -> PlexItem | None:
        return self._items.get(rating_key)

    def list_collections(self) -> list[PlexCollection]:
        return self._collections

    def get_user_data(self, rating_key: int, *, token: str | None = None) -> PlexUserData | None:
        if rating_key not in self._items:
            return None
        return self._user_data_by_rating_key.get(rating_key, PlexUserData())


def _media_path(filename: str) -> str:
    return f"{MEDIA_ROOT}/{filename}"


def _build_item(
    *,
    rating_key: int,
    filename: str,
    studio: str,
    writer: str,
    director: str,
    collections: tuple[str, ...] = (),
    path_prefix: str = MEDIA_ROOT,
) -> PlexItem:
    path = f"{path_prefix.rstrip('/')}/{filename}"
    return PlexItem(
        rating_key=rating_key,
        path=path,
        paths=(path,),
        studio=studio,
        writers=(writer,),
        directors=(director,),
        collections=collections,
    )


def _build_merged_item(
    *,
    rating_key: int,
    filenames: tuple[str, ...],
    studio: str,
    writer: str,
    director: str,
    path_prefix: str = MEDIA_ROOT,
) -> PlexItem:
    paths = tuple(f"{path_prefix.rstrip('/')}/{filename}" for filename in filenames)
    return PlexItem(
        rating_key=rating_key,
        path=paths[0],
        paths=paths,
        studio=studio,
        writers=(writer,),
        directors=(director,),
    )


def _build_config(
    tmp_path: Path,
    harness: dict[str, object],
    *,
    include_user_mapping: bool = False,
    webhook_shared_secret: str | None = None,
    debounce_seconds: int | None = None,
    full_sync_debounce_seconds: int | None = None,
    user_data_debounce_seconds: int | None = None,
    path_mapping_rules: list[dict[str, str]] | None = None,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "plex": {
                "base_url": "http://plex:32400",
                "token": "test-plex-token",
                "library_name": "Other Video",
            },
            "jellyfin": {
                "base_url": str(harness["base_url"]),
                "api_key": str(harness["api_key"]),
                "user_id": str(harness["user_id"]),
                "library_name": "Other Video",
            },
            "user_mapping": (
                [
                    {
                        "plex_account": "plex-user",
                        "jellyfin_user_id": str(harness["user_id"]),
                    }
                ]
                if include_user_mapping
                else []
            ),
            "sync": (
                {
                    key: value
                    for key, value in {
                        "debounce_seconds": debounce_seconds,
                        "full_sync_debounce_seconds": full_sync_debounce_seconds,
                        "user_data_debounce_seconds": user_data_debounce_seconds,
                    }.items()
                    if value is not None
                }
            ),
            "webhook": (
                {"shared_secret": webhook_shared_secret}
                if webhook_shared_secret is not None
                else {}
            ),
            "path_mapping": {"rules": path_mapping_rules or []},
            "state": {"sqlite_path": str(tmp_path / "sync.db")},
        }
    )


def _build_engine(
    tmp_path: Path,
    harness: dict[str, object],
    *,
    items: list[PlexItem],
    collections: list[PlexCollection] | None = None,
    user_data_by_rating_key: dict[int, PlexUserData] | None = None,
    include_user_mapping: bool = False,
    webhook_shared_secret: str | None = None,
    user_data_debounce_seconds: int | None = None,
    path_mapping_rules: list[dict[str, str]] | None = None,
) -> tuple[SyncEngine, JellyfinClient]:
    config = _build_config(
        tmp_path,
        harness,
        include_user_mapping=include_user_mapping,
        webhook_shared_secret=webhook_shared_secret,
        user_data_debounce_seconds=user_data_debounce_seconds,
        path_mapping_rules=path_mapping_rules,
    )
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    jellyfin = JellyfinClient(
        base_url=config.jellyfin.base_url,
        api_key=config.jellyfin.api_key,
        library_name=config.jellyfin.library_name,
        user_id=config.jellyfin.user_id,
    )
    engine = SyncEngine(
        config=config,
        state=state,
        plex=FunctionalPlexClient(
            items,
            collections=collections,
            user_data_by_rating_key=user_data_by_rating_key,
        ),
        jellyfin=jellyfin,
        path_mapper=PathMapper(config.path_mapping.rules),
    )
    return engine, jellyfin


def _build_webhook_stack(
    tmp_path: Path,
    harness: dict[str, object],
    *,
    items: list[PlexItem],
    user_data_by_rating_key: dict[int, PlexUserData],
    debounce_seconds: int = 0,
    full_sync_debounce_seconds: int = 0,
    user_data_debounce_seconds: int = 0,
    event_log: list[tuple[str, str, float]] | None = None,
    refresh_log: list[float] | None = None,
    block_first_manual_full: tuple[asyncio.Event, asyncio.Event] | None = None,
) -> tuple[AppConfig, StateStore, JellyfinClient, DebounceQueue, httpx.ASGITransport, JobTracker]:
    config = _build_config(
        tmp_path,
        harness,
        include_user_mapping=True,
        webhook_shared_secret="secret",
        debounce_seconds=debounce_seconds,
        full_sync_debounce_seconds=full_sync_debounce_seconds,
        user_data_debounce_seconds=user_data_debounce_seconds,
    )
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    jellyfin = JellyfinClient(
        base_url=config.jellyfin.base_url,
        api_key=config.jellyfin.api_key,
        library_name=config.jellyfin.library_name,
        user_id=config.jellyfin.user_id,
    )
    original_trigger_library_refresh = jellyfin.trigger_library_refresh

    def tracked_trigger_library_refresh() -> None:
        if refresh_log is not None:
            refresh_log.append(time.monotonic())
        original_trigger_library_refresh()

    jellyfin.trigger_library_refresh = tracked_trigger_library_refresh  # type: ignore[method-assign]
    queue: DebounceQueue | None = None

    def requeue_event(event: SyncEvent) -> bool:
        if queue is None:
            return False
        if event.kind == "userdata" and event.rating_key is not None and event.jellyfin_user_id is not None:
            if event.requeue_count >= config.sync.merging.max_requeue_count:
                return False
            account = event.plex_account or ""
            return queue.submit_user_data_sync(
                event.rating_key,
                plex_account=account,
                jellyfin_user_id=event.jellyfin_user_id,
                requeue_count=event.requeue_count,
            )
        if event.kind == "item" and event.rating_key is not None:
            return queue.submit_item_sync(event.rating_key, requeue_count=event.requeue_count)
        return False

    engine = SyncEngine(
        config=config,
        state=state,
        plex=FunctionalPlexClient(items, user_data_by_rating_key=user_data_by_rating_key),
        jellyfin=jellyfin,
        requeue_callback=requeue_event,
    )

    async def handle_event(event: SyncEvent):
        if event_log is not None:
            event_log.append((event.kind, event.source, time.monotonic()))
        if (
            block_first_manual_full is not None
            and event.kind == "full"
            and event.source == "manual"
            and not block_first_manual_full[0].is_set()
        ):
            block_first_manual_full[0].set()
            await block_first_manual_full[1].wait()
        return await engine.handle_event(event)

    queue = DebounceQueue(
        handle_event,
        debounce_seconds=config.sync.debounce_seconds,
        full_sync_debounce_seconds=config.sync.full_sync_debounce_seconds,
        user_data_debounce_seconds=config.sync.user_data_debounce_seconds,
        max_requeue_count=config.sync.merging.max_requeue_count,
    )
    job_tracker = JobTracker(jobs={})
    original_handler = queue._handler
    app = create_app(
        config=config,
        queue=queue,
        job_tracker=job_tracker,
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    async def tracked_handler(event: SyncEvent) -> None:
        if event.job_id:
            job_tracker.set(event.job_id, "running")
        try:
            result = await original_handler(event)
        except Exception as exc:
            if event.job_id:
                job_tracker.set(event.job_id, "failed", error=str(exc))
            raise
        else:
            if event.job_id and result is not None:
                job_tracker.set(
                    event.job_id,
                    "complete",
                    result={
                        "scope": result.scope,
                        "started_at": result.started_at.isoformat(),
                        "completed_at": result.completed_at.isoformat(),
                        "duration_ms": result.duration_ms,
                        "items_examined": result.items_examined,
                        "items_updated": result.items_updated,
                        "user_data_updated": result.user_data_updated,
                        "merges_applied": result.merges_applied,
                        "unmerges_applied": result.unmerges_applied,
                        "requeued_events": result.requeued_events,
                        "errors": result.errors,
                    },
                )

    queue._handler = tracked_handler
    return config, state, jellyfin, queue, httpx.ASGITransport(app=app), job_tracker


def _reset_harness_library(client: JellyfinClient) -> None:
    for collection in client.list_collections():
        client.delete_item(collection.collection_id)
    for item in client.list_library_items():
        client.update_item_metadata(item.item_id, DesiredMetadata())


def _item_by_path(client: JellyfinClient, path: str):
    item = client.find_item_by_path(path)
    assert item is not None, f"expected Jellyfin item for path {path}"
    return client.get_item(item.item_id)


def _collection_member_paths(client: JellyfinClient, name: str) -> set[str]:
    collection = next((entry for entry in client.list_collections() if entry.name == name), None)
    assert collection is not None, f"expected Jellyfin collection {name}"
    return {client.get_item(item_id).path for item_id in collection.item_ids}


def _wait_for_collection_member_paths(
    client: JellyfinClient,
    name: str,
    expected_paths: set[str],
    *,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _collection_member_paths(client, name) == expected_paths:
            return
        time.sleep(0.5)
    assert _collection_member_paths(client, name) == expected_paths


def _wait_for_collection_absence(client: JellyfinClient, name: str, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if all(collection.name != name for collection in client.list_collections()):
            return
        time.sleep(0.5)
    assert all(collection.name != name for collection in client.list_collections())


def _wait_for_merged_item(
    client: JellyfinClient,
    primary_path: str,
    expected_paths: set[str],
    *,
    timeout_seconds: float = 10.0,
):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        item = _item_by_path(client, primary_path)
        media_paths = {path for path, _ in item.media_sources}
        if media_paths == expected_paths:
            return item
        time.sleep(0.5)
    item = _item_by_path(client, primary_path)
    assert {path for path, _ in item.media_sources} == expected_paths
    return item


def _user_data_by_path(client: JellyfinClient, user_id: str, path: str):
    item = _item_by_path(client, path)
    return client.get_user_data(user_id, item.item_id)


def _set_user_data_state(
    client: JellyfinClient,
    user_id: str,
    path: str,
    *,
    played: bool | object = UNSET,
    play_count: int | object = UNSET,
    rating: float | None | object = UNSET,
    last_played_date: datetime | None | object = UNSET,
) -> None:
    item = _item_by_path(client, path)
    if played is not UNSET:
        if played:
            client.mark_played(user_id, item.item_id)
        else:
            client.mark_unplayed(user_id, item.item_id)
    client.update_user_data(
        user_id,
        item.item_id,
        play_count=None if play_count is UNSET else play_count,
        rating=None if rating is UNSET else rating,
        last_played_date=None if last_played_date is UNSET else last_played_date,
    )


def _wait_for_user_data(
    client: JellyfinClient,
    user_id: str,
    path: str,
    *,
    played: bool | object = UNSET,
    play_count: int | object = UNSET,
    rating: float | None | object = UNSET,
    last_played_date: datetime | None | object = UNSET,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = _user_data_by_path(client, user_id, path)
        if (
            (played is UNSET or current.played == played)
            and (play_count is UNSET or current.play_count == play_count)
            and (rating is UNSET or current.rating == rating)
            and (last_played_date is UNSET or current.last_played_date == last_played_date)
        ):
            return
        time.sleep(0.5)
    current = _user_data_by_path(client, user_id, path)
    if played is not UNSET:
        assert current.played == played
    if play_count is not UNSET:
        assert current.play_count == play_count
    if rating is not UNSET:
        assert current.rating == rating
    if last_played_date is not UNSET:
        assert current.last_played_date == last_played_date


async def _seed_item_metadata(
    tmp_path: Path,
    harness: dict[str, object],
    item: PlexItem,
) -> JellyfinClient:
    engine, jellyfin = _build_engine(tmp_path, harness, items=[item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=item.rating_key))
    return jellyfin


async def _true_async() -> bool:
    return True


async def _wait_for_job_status(job_tracker: JobTracker, job_id: str, status: str, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = job_tracker.get(job_id)
        if record is not None and record.status == status:
            return
        await asyncio.sleep(0.05)
    record = job_tracker.get(job_id)
    assert record is not None
    assert record.status == status


async def _wait_for_event_count(event_log: list[tuple[str, str, float]], expected_count: int, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if len(event_log) >= expected_count:
            return
        await asyncio.sleep(0.05)
    assert len(event_log) >= expected_count


def _wait_for_library_item_count(client: JellyfinClient, expected_count: int, *, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        client.trigger_library_refresh()
        if len(client.list_library_items()) == expected_count:
            return
        time.sleep(0.5)
    assert len(client.list_library_items()) == expected_count


def _compose_control(harness: dict[str, object], *args: str) -> subprocess.CompletedProcess[str]:
    compose_file = Path(str(harness["compose_file"]))
    workspace = Path(str(harness["workspace"]))
    return subprocess.run(
        ["docker", "compose", "-f", str(compose_file), *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=workspace,
    )


def _wait_for_harness_ready(client: JellyfinClient, expected_count: int, *, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            _wait_for_library_item_count(client, expected_count, timeout_seconds=2.0)
            return
        except Exception:
            time.sleep(1.0)
    _wait_for_library_item_count(client, expected_count, timeout_seconds=5.0)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_full_sync_from_empty_jellyfin(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    items = [
        _build_item(
            rating_key=1,
            filename="fixture-01.mp4",
            studio="Studio One",
            writer="Alice",
            director="Bob",
            collections=("Favorites",),
        ),
        _build_item(
            rating_key=2,
            filename="fixture-02.mp4",
            studio="Studio Two",
            writer="Carol",
            director="Dan",
            collections=("Favorites",),
        ),
        _build_item(
            rating_key=3,
            filename="fixture-03.mp4",
            studio="Studio Three",
            writer="Eve",
            director="Frank",
            collections=("Favorites",),
        ),
    ]
    collections = [PlexCollection(key=100, name="Favorites", member_rating_keys=(1, 2, 3))]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items, collections=collections)

    result = await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert result.items_examined == 3
    assert result.items_updated == 3
    for item in items:
        synced = _item_by_path(jellyfin, item.path)
        assert synced.metadata.studios == (item.studio,)
        assert {(person.name, person.role) for person in synced.metadata.people} == {
            (item.writers[0], "Actor"),
            (item.directors[0], "Director"),
        }
        assert set(synced.metadata.locked_fields) == {"Cast", "Studios"}

    synced_collections = {collection.name: collection for collection in jellyfin.list_collections()}
    assert "Favorites" in synced_collections
    assert len(synced_collections["Favorites"].item_ids) == 3


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_idempotent_full_sync_skips_second_write_pass(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    items = [
        _build_item(
            rating_key=1,
            filename="fixture-01.mp4",
            studio="Studio One",
            writer="Alice",
            director="Bob",
            collections=("Favorites",),
        ),
        _build_item(
            rating_key=2,
            filename="fixture-02.mp4",
            studio="Studio Two",
            writer="Carol",
            director="Dan",
            collections=("Favorites",),
        ),
    ]
    collections = [PlexCollection(key=100, name="Favorites", member_rating_keys=(1, 2))]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items, collections=collections)

    first = await engine.handle_event(SyncEvent(kind="full", source="manual"))
    second = await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert first.items_updated == 2
    assert second.items_examined == 2
    assert second.items_updated == 0
    assert {collection.name for collection in jellyfin.list_collections()} == {"Favorites"}


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_studio_change_updates_real_jellyfin_item(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    initial_item = _build_item(
        rating_key=1,
        filename="fixture-01.mp4",
        studio="Studio One",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[initial_item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    updated_item = _build_item(
        rating_key=1,
        filename="fixture-01.mp4",
        studio="Studio Renamed",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[updated_item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    synced = _item_by_path(jellyfin, updated_item.path)
    assert synced.metadata.studios == ("Studio Renamed",)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_add_writer_updates_real_jellyfin_people(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    initial_item = _build_item(
        rating_key=1,
        filename="fixture-01.mp4",
        studio="Studio One",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[initial_item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    updated_item = PlexItem(
        rating_key=1,
        path=_media_path("fixture-01.mp4"),
        paths=(_media_path("fixture-01.mp4"),),
        studio="Studio One",
        writers=("Alice", "Carol"),
        directors=("Bob",),
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[updated_item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    synced = _item_by_path(jellyfin, updated_item.path)
    assert {(person.name, person.role) for person in synced.metadata.people} == {
        ("Alice", "Actor"),
        ("Carol", "Actor"),
        ("Bob", "Director"),
    }


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_remove_writer_updates_real_jellyfin_people(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    initial_item = PlexItem(
        rating_key=1,
        path=_media_path("fixture-01.mp4"),
        paths=(_media_path("fixture-01.mp4"),),
        studio="Studio One",
        writers=("Alice", "Carol"),
        directors=("Bob",),
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[initial_item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    updated_item = _build_item(
        rating_key=1,
        filename="fixture-01.mp4",
        studio="Studio One",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[updated_item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    synced = _item_by_path(jellyfin, updated_item.path)
    assert {(person.name, person.role) for person in synced.metadata.people} == {
        ("Alice", "Actor"),
        ("Bob", "Director"),
    }


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_sync_sets_locked_fields_on_real_jellyfin_item(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_item(
        rating_key=1,
        filename="fixture-01.mp4",
        studio="Studio One",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[item])

    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    synced = _item_by_path(jellyfin, item.path)
    assert set(synced.metadata.locked_fields) == {"Cast", "Studios"}


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_collection_creation_creates_real_boxset(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    items = [
        _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob"),
        _build_item(rating_key=2, filename="fixture-02.mp4", studio="Studio Two", writer="Carol", director="Dan"),
        _build_item(rating_key=3, filename="fixture-03.mp4", studio="Studio Three", writer="Eve", director="Frank"),
    ]
    collections = [PlexCollection(key=200, name="Collection Creation", member_rating_keys=(1, 3))]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items, collections=collections)

    await engine.handle_event(SyncEvent(kind="full", source="manual"))

    _wait_for_collection_member_paths(jellyfin, "Collection Creation", {
        _media_path("fixture-01.mp4"),
        _media_path("fixture-03.mp4"),
    })


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_collection_membership_change_updates_real_boxsets(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    items = [
        _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob"),
        _build_item(rating_key=2, filename="fixture-02.mp4", studio="Studio Two", writer="Carol", director="Dan"),
        _build_item(rating_key=3, filename="fixture-03.mp4", studio="Studio Three", writer="Eve", director="Frank"),
    ]
    initial_collections = [
        PlexCollection(key=201, name="Favorites", member_rating_keys=(1, 2)),
        PlexCollection(key=202, name="Watchlist", member_rating_keys=(3,)),
    ]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items, collections=initial_collections)
    await engine.handle_event(SyncEvent(kind="full", source="manual"))

    updated_collections = [
        PlexCollection(key=201, name="Favorites", member_rating_keys=(1,)),
        PlexCollection(key=202, name="Watchlist", member_rating_keys=(2, 3)),
    ]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items, collections=updated_collections)
    await engine.handle_event(SyncEvent(kind="full", source="manual"))

    _wait_for_collection_member_paths(jellyfin, "Favorites", {_media_path("fixture-01.mp4")})
    _wait_for_collection_member_paths(jellyfin, "Watchlist", {
        _media_path("fixture-02.mp4"),
        _media_path("fixture-03.mp4"),
    })


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_collection_deletion_removes_real_boxset(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    items = [
        _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob"),
        _build_item(rating_key=2, filename="fixture-02.mp4", studio="Studio Two", writer="Carol", director="Dan"),
    ]
    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=items,
        collections=[PlexCollection(key=203, name="Temporary BoxSet", member_rating_keys=(1, 2))],
    )
    await engine.handle_event(SyncEvent(kind="full", source="manual"))
    _wait_for_collection_member_paths(jellyfin, "Temporary BoxSet", {
        _media_path("fixture-01.mp4"),
        _media_path("fixture-02.mp4"),
    })

    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items, collections=[])
    await engine.handle_event(SyncEvent(kind="full", source="manual"))

    _wait_for_collection_absence(jellyfin, "Temporary BoxSet")


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_smart_collection_materializes_expected_members(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    items = [
        _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob"),
        _build_item(rating_key=2, filename="fixture-02.mp4", studio="Studio Two", writer="Carol", director="Dan"),
        _build_item(rating_key=3, filename="fixture-03.mp4", studio="Studio Three", writer="Eve", director="Frank"),
    ]
    smart_collection = PlexCollection(key=204, name="Smart Picks", member_rating_keys=(1, 3))
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items, collections=[smart_collection])

    await engine.handle_event(SyncEvent(kind="full", source="manual"))

    _wait_for_collection_member_paths(jellyfin, "Smart Picks", {
        _media_path("fixture-01.mp4"),
        _media_path("fixture-03.mp4"),
    })


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_watched_state_promotion(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, played=False, play_count=0)
    _wait_for_user_data(jellyfin, user_id, item.path, played=False, play_count=0)

    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(watched=True, play_count=1)},
        include_user_mapping=True,
    )
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=1)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_watched_state_preservation(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, played=True, play_count=3)
    _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=3)

    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(watched=False, play_count=0)},
        include_user_mapping=True,
    )
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=3)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_play_count_promotion(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, played=True, play_count=2)
    _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=2)

    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(watched=True, play_count=5)},
        include_user_mapping=True,
    )
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=5)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_play_count_preservation(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, played=True, play_count=5)
    _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=5)

    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(watched=True, play_count=2)},
        include_user_mapping=True,
    )
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=5)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_rating_update(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, rating=6.0)
    _wait_for_user_data(jellyfin, user_id, item.path, rating=6.0)

    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(rating=8.0)},
        include_user_mapping=True,
    )
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    _wait_for_user_data(jellyfin, user_id, item.path, rating=8.0)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_rating_cleared_in_plex_preserves_jellyfin_rating(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, rating=7.0)
    _wait_for_user_data(jellyfin, user_id, item.path, rating=7.0)

    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(rating=None)},
        include_user_mapping=True,
    )
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    _wait_for_user_data(jellyfin, user_id, item.path, rating=7.0)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_last_played_monotonicity(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    existing_last_played = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)
    _set_user_data_state(
        jellyfin,
        user_id,
        item.path,
        played=True,
        play_count=3,
        last_played_date=existing_last_played,
    )
    _wait_for_user_data(
        jellyfin,
        user_id,
        item.path,
        played=True,
        play_count=3,
        last_played_date=existing_last_played,
    )


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_user_data_webhook_updates_mapped_user(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, played=False, play_count=0)
    _wait_for_user_data(jellyfin, user_id, item.path, played=False, play_count=0)

    _, state, jellyfin, queue, transport, _ = _build_webhook_stack(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(watched=True, play_count=1)},
    )

    try:
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            response = await webhook_client.post(
                "/webhook/plex",
                json={
                    "event": "media.scrobble",
                    "Metadata": {"ratingKey": "1"},
                    "Account": {"title": "plex-user"},
                },
                headers={"x-webhook-secret": "secret"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        await queue.wait_for_idle(timeout=3.0)
        _wait_for_user_data(jellyfin, user_id, item.path, played=True, play_count=1)
    finally:
        await queue.stop()
        state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_unmapped_account_webhook_is_dropped(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    jellyfin = await _seed_item_metadata(tmp_path, functional_harness, item)
    _set_user_data_state(jellyfin, user_id, item.path, played=False, play_count=0)
    _wait_for_user_data(jellyfin, user_id, item.path, played=False, play_count=0)

    _, state, jellyfin, queue, transport, _ = _build_webhook_stack(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(watched=True, play_count=1)},
    )

    try:
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            response = await webhook_client.post(
                "/webhook/plex",
                json={
                    "event": "media.scrobble",
                    "Metadata": {"ratingKey": "1"},
                    "Account": {"title": "someone-else"},
                },
                headers={"x-webhook-secret": "secret"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        await queue.wait_for_idle(timeout=1.0)
        _wait_for_user_data(jellyfin, user_id, item.path, played=False, play_count=0)
    finally:
        await queue.stop()
        state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_manual_trigger_idle_starts_full_sync_and_records_job(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    _, state, _, queue, transport, job_tracker = _build_webhook_stack(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={},
    )

    try:
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            response = await webhook_client.post("/trigger/full-sync")
            assert response.status_code == 202
            job_id = response.json()["job_id"]

            await queue.wait_for_idle(timeout=5.0)
            await _wait_for_job_status(job_tracker, job_id, "complete")

            status_response = await webhook_client.get(f"/trigger/status/{job_id}")
            assert status_response.status_code == 200
            assert status_response.json()["status"] == "complete"
            assert status_response.json()["result"]["items_examined"] == 1
    finally:
        await queue.stop()
        state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_manual_trigger_clears_pending_item_queue(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    event_log: list[tuple[str, str, float]] = []
    _, state, _, queue, transport, _ = _build_webhook_stack(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={},
        debounce_seconds=1,
        event_log=event_log,
    )

    try:
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            for rating_key in range(1, 6):
                response = await webhook_client.post(
                    "/webhook/plex",
                    json={"event": "library.new", "Metadata": {"ratingKey": str(rating_key)}},
                    headers={"x-webhook-secret": "secret"},
                )
                assert response.status_code == 200

            trigger_response = await webhook_client.post("/trigger/full-sync")
            assert trigger_response.status_code == 202

        await queue.wait_for_idle(timeout=5.0)
        assert [(kind, source) for kind, source, _ in event_log] == [("full", "manual")]
    finally:
        await queue.stop()
        state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_manual_trigger_during_active_sync_runs_once_next_and_clears_pending_work(
    functional_harness,
    tmp_path: Path,
) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    event_log: list[tuple[str, str, float]] = []
    first_full_started = asyncio.Event()
    release_first_full = asyncio.Event()
    _, state, _, queue, transport, job_tracker = _build_webhook_stack(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={},
        debounce_seconds=1,
        event_log=event_log,
        block_first_manual_full=(first_full_started, release_first_full),
    )

    try:
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            first_response = await webhook_client.post("/trigger/full-sync")
            assert first_response.status_code == 202
            first_job_id = first_response.json()["job_id"]

            await asyncio.wait_for(first_full_started.wait(), timeout=3.0)

            second_response = await webhook_client.post("/trigger/full-sync")
            assert second_response.status_code == 202
            second_job_id = second_response.json()["job_id"]

            for rating_key in range(1, 4):
                response = await webhook_client.post(
                    "/webhook/plex",
                    json={"event": "library.new", "Metadata": {"ratingKey": str(rating_key)}},
                    headers={"x-webhook-secret": "secret"},
                )
                assert response.status_code == 200

            await asyncio.sleep(1.2)
            release_first_full.set()

            await queue.wait_for_idle(timeout=6.0)
            await _wait_for_job_status(job_tracker, first_job_id, "complete")
            await _wait_for_job_status(job_tracker, second_job_id, "complete")

        assert [(kind, source) for kind, source, _ in event_log] == [
            ("full", "manual"),
            ("full", "manual"),
        ]
    finally:
        release_first_full.set()
        await queue.stop()
        state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_library_new_webhook_respects_item_debounce(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    event_log: list[tuple[str, str, float]] = []
    _, state, _, queue, transport, _ = _build_webhook_stack(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={},
        debounce_seconds=1,
        event_log=event_log,
    )

    try:
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            submitted_at = time.monotonic()
            response = await webhook_client.post(
                "/webhook/plex",
                json={"event": "library.new", "Metadata": {"ratingKey": "1"}},
                headers={"x-webhook-secret": "secret"},
            )
            assert response.status_code == 200

        await queue.wait_for_idle(timeout=5.0)
        assert [(kind, source) for kind, source, _ in event_log] == [("item", "webhook")]
        assert event_log[0][2] - submitted_at >= 0.9
    finally:
        await queue.stop()
        state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_library_new_debounce_resets_on_second_webhook(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob")
    event_log: list[tuple[str, str, float]] = []
    _, state, _, queue, transport, _ = _build_webhook_stack(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={},
        debounce_seconds=1,
        event_log=event_log,
    )

    try:
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            first_response = await webhook_client.post(
                "/webhook/plex",
                json={"event": "library.new", "Metadata": {"ratingKey": "1"}},
                headers={"x-webhook-secret": "secret"},
            )
            assert first_response.status_code == 200
            await asyncio.sleep(0.4)
            second_submitted_at = time.monotonic()
            second_response = await webhook_client.post(
                "/webhook/plex",
                json={"event": "library.new", "Metadata": {"ratingKey": "1"}},
                headers={"x-webhook-secret": "secret"},
            )
            assert second_response.status_code == 200

        await queue.wait_for_idle(timeout=5.0)
        assert [(kind, source) for kind, source, _ in event_log] == [("item", "webhook")]
        assert event_log[0][2] - second_submitted_at >= 0.9
    finally:
        await queue.stop()
        state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_path_mapping_matches_items_across_different_prefixes(
    functional_harness,
    tmp_path: Path,
) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_item(
        rating_key=1,
        filename="fixture-01.mp4",
        studio="Studio One",
        writer="Alice",
        director="Bob",
        path_prefix="/plex/other-video",
    )
    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        path_mapping_rules=[
            {
                "plex_prefix": "/plex/other-video",
                "jellyfin_prefix": MEDIA_ROOT,
            }
        ],
    )

    result = await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    assert result.items_examined == 1
    assert result.items_updated == 1
    synced = _item_by_path(jellyfin, _media_path("fixture-01.mp4"))
    assert synced.metadata.studios == ("Studio One",)
    assert {(person.name, person.role) for person in synced.metadata.people} == {
        ("Alice", "Actor"),
        ("Bob", "Director"),
    }


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_initial_merge_creates_single_primary_with_all_sources(
    functional_harness,
    tmp_path: Path,
) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[item])

    result = await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    assert result.items_examined == 1
    assert result.items_updated == 1
    assert result.merges_applied == 1
    merged = _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
            _media_path("fixture-03.mp4"),
        },
    )
    assert merged.path == _media_path("fixture-01.mp4")
    assert merged.metadata.studios == ("Merged Studio",)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_merged_item_idempotency_skips_second_merge(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[item])
    first = await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))
    _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
            _media_path("fixture-03.mp4"),
        },
    )

    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[item])
    second = await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    assert first.merges_applied == 1
    assert second.items_updated == 0
    assert second.merges_applied == 0


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_merged_item_metadata_update_writes_primary_only(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))
    _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
            _media_path("fixture-03.mp4"),
        },
    )

    updated = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio Renamed",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[updated])
    result = await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    assert result.items_updated == 1
    merged = _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
            _media_path("fixture-03.mp4"),
        },
    )
    assert merged.metadata.studios == ("Merged Studio Renamed",)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_merged_item_watched_state_is_preserved(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    user_id = str(functional_harness["user_id"])

    item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))
    merged = _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
            _media_path("fixture-03.mp4"),
        },
    )
    jellyfin.mark_played(user_id, merged.item_id)
    jellyfin.update_user_data(user_id, merged.item_id, play_count=3, rating=None, last_played_date=None)
    _wait_for_user_data(jellyfin, user_id, _media_path("fixture-01.mp4"), played=True, play_count=3)

    engine, jellyfin = _build_engine(
        tmp_path,
        functional_harness,
        items=[item],
        user_data_by_rating_key={1: PlexUserData(watched=False, play_count=0)},
        include_user_mapping=True,
    )
    result = await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    assert result.user_data_updated == 0
    _wait_for_user_data(jellyfin, user_id, _media_path("fixture-01.mp4"), played=True, play_count=3)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_plex_unmerge_propagates_back_to_separate_items(functional_harness, tmp_path: Path) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    merged_item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[merged_item])
    await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))
    _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
            _media_path("fixture-03.mp4"),
        },
    )

    split_items = [
        _build_item(rating_key=1, filename="fixture-01.mp4", studio="Split One", writer="Alice", director="Bob"),
        _build_item(rating_key=2, filename="fixture-02.mp4", studio="Split Two", writer="Carol", director="Dan"),
        _build_item(rating_key=3, filename="fixture-03.mp4", studio="Split Three", writer="Eve", director="Frank"),
    ]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=split_items)
    result = await engine.handle_event(SyncEvent(kind="full", source="manual"))

    assert result.unmerges_applied >= 1
    for item in split_items:
        synced = _item_by_path(jellyfin, item.path)
        assert synced.media_sources == ((item.path, synced.item_id),)
        assert synced.metadata.studios == (item.studio,)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_jellyfin_grouping_disagreement_is_rebuilt_to_match_plex(
    functional_harness,
    tmp_path: Path,
) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    split_items = [
        _build_item(rating_key=1, filename="fixture-01.mp4", studio="Split One", writer="Alice", director="Bob"),
        _build_item(rating_key=2, filename="fixture-02.mp4", studio="Split Two", writer="Carol", director="Dan"),
        _build_item(rating_key=3, filename="fixture-03.mp4", studio="Split Three", writer="Eve", director="Frank"),
    ]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=split_items)
    await engine.handle_event(SyncEvent(kind="full", source="manual"))

    first = _item_by_path(jellyfin, _media_path("fixture-01.mp4"))
    second = _item_by_path(jellyfin, _media_path("fixture-02.mp4"))
    jellyfin.merge_versions((first.item_id, second.item_id))
    _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
        },
    )

    merged_item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=[merged_item])
    result = await engine.handle_event(SyncEvent(kind="item", source="manual", rating_key=1))

    assert result.merges_applied == 1
    assert result.unmerges_applied == 1
    merged = _wait_for_merged_item(
        jellyfin,
        _media_path("fixture-01.mp4"),
        {
            _media_path("fixture-01.mp4"),
            _media_path("fixture-02.mp4"),
            _media_path("fixture-03.mp4"),
        },
    )
    assert merged.metadata.studios == ("Merged Studio",)


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_deferred_merge_requeues_until_missing_file_appears(
    functional_harness,
    tmp_path: Path,
) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    media_dir = Path(str(functional_harness["media_dir"]))
    missing_path = media_dir / "fixture-03.mp4"
    hidden_path = media_dir / "fixture-03.delayed"
    event_log: list[tuple[str, str, float]] = []
    refresh_log: list[float] = []
    merged_item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    state: StateStore | None = None
    queue: DebounceQueue | None = None
    try:
        missing_path.rename(hidden_path)
        _wait_for_library_item_count(client, 2)
        _, state, jellyfin, queue, transport, _ = _build_webhook_stack(
            tmp_path,
            functional_harness,
            items=[merged_item],
            user_data_by_rating_key={},
            debounce_seconds=1,
            refresh_log=refresh_log,
            event_log=event_log,
        )
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            response = await webhook_client.post(
                "/webhook/plex",
                json={"event": "library.new", "Metadata": {"ratingKey": "1"}},
                headers={"x-webhook-secret": "secret"},
            )
            assert response.status_code == 200

        await _wait_for_event_count(event_log, 1, timeout_seconds=5.0)
        assert len(refresh_log) == 1

        hidden_path.rename(missing_path)
        _wait_for_library_item_count(client, 3)

        await queue.wait_for_idle(timeout=10.0)

        assert [(kind, source) for kind, source, _ in event_log] == [
            ("item", "webhook"),
            ("item", "webhook"),
        ]
        assert len(refresh_log) == 1
        merged = _wait_for_merged_item(
            jellyfin,
            _media_path("fixture-01.mp4"),
            {
                _media_path("fixture-01.mp4"),
                _media_path("fixture-02.mp4"),
                _media_path("fixture-03.mp4"),
            },
        )
        assert merged.metadata.studios == ("Merged Studio",)
        recent_logs = [record for record in state.list_recent_sync_logs(limit=10) if record.scope == "item:1"]
        assert len(recent_logs) >= 2
        assert any(record.requeued_events == 1 for record in recent_logs)
        assert recent_logs[0].errors == 0
    finally:
        if hidden_path.exists():
            hidden_path.rename(missing_path)
            _wait_for_library_item_count(client, 3)
        if queue is not None:
            await queue.stop()
        if state is not None:
            state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_requeue_exhaustion_drops_event_and_records_hard_error(
    functional_harness,
    tmp_path: Path,
) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)
    media_dir = Path(str(functional_harness["media_dir"]))
    missing_path = media_dir / "fixture-03.mp4"
    hidden_path = media_dir / "fixture-03.delayed"
    event_log: list[tuple[str, str, float]] = []
    refresh_log: list[float] = []
    merged_item = _build_merged_item(
        rating_key=1,
        filenames=("fixture-01.mp4", "fixture-02.mp4", "fixture-03.mp4"),
        studio="Merged Studio",
        writer="Alice",
        director="Bob",
    )
    state: StateStore | None = None
    queue: DebounceQueue | None = None
    try:
        missing_path.rename(hidden_path)
        _wait_for_library_item_count(client, 2)
        _, state, _, queue, transport, _ = _build_webhook_stack(
            tmp_path,
            functional_harness,
            items=[merged_item],
            user_data_by_rating_key={},
            debounce_seconds=1,
            refresh_log=refresh_log,
            event_log=event_log,
        )
        await queue.start()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as webhook_client:
            response = await webhook_client.post(
                "/webhook/plex",
                json={"event": "library.new", "Metadata": {"ratingKey": "1"}},
                headers={"x-webhook-secret": "secret"},
            )
            assert response.status_code == 200

        await queue.wait_for_idle(timeout=10.0)

        assert [(kind, source) for kind, source, _ in event_log] == [
            ("item", "webhook"),
            ("item", "webhook"),
            ("item", "webhook"),
        ]
        assert len(refresh_log) == 3
        recent_logs = [record for record in state.list_recent_sync_logs(limit=10) if record.scope == "item:1"]
        assert len(recent_logs) == 3
        assert sum(1 for record in recent_logs if record.requeued_events == 1) == 2
        assert recent_logs[0].errors == 1
        assert recent_logs[0].error_detail is not None
        assert "could not be requeued" in recent_logs[0].error_detail
    finally:
        if hidden_path.exists():
            hidden_path.rename(missing_path)
            _wait_for_library_item_count(client, 3)
        if queue is not None:
            await queue.stop()
        if state is not None:
            state.close()


@pytest.mark.asyncio
@pytest.mark.functional_harness
async def test_functional_matrix_restart_recovery_survives_mid_sync_jellyfin_restart(
    functional_harness,
    tmp_path: Path,
) -> None:
    client = functional_harness["client"]
    assert isinstance(client, JellyfinClient)
    _reset_harness_library(client)

    items = [
        _build_item(rating_key=1, filename="fixture-01.mp4", studio="Studio One", writer="Alice", director="Bob"),
        _build_item(rating_key=2, filename="fixture-02.mp4", studio="Studio Two", writer="Carol", director="Dan"),
        _build_item(rating_key=3, filename="fixture-03.mp4", studio="Studio Three", writer="Eve", director="Frank"),
    ]
    engine, jellyfin = _build_engine(tmp_path, functional_harness, items=items)
    jellyfin._request_timeout_seconds = 1.0
    jellyfin._max_retries = 0
    original_sync_item = engine._sync_item
    block_second_item = threading.Event()
    release_second_item = threading.Event()
    sync_calls = 0

    def blocking_sync_item(item: PlexItem) -> tuple[bool, str]:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            block_second_item.set()
            if not release_second_item.wait(timeout=15.0):
                raise TimeoutError("timed out waiting to resume the second item sync")
        return original_sync_item(item)

    engine._sync_item = blocking_sync_item  # type: ignore[method-assign]

    async def run_full_sync() -> object:
        return await asyncio.to_thread(
            lambda: asyncio.run(engine.handle_event(SyncEvent(kind="full", source="manual")))
        )

    sync_task = asyncio.create_task(run_full_sync())
    restart_result: subprocess.CompletedProcess[str] | None = None
    try:
        await asyncio.wait_for(asyncio.to_thread(block_second_item.wait, 10.0), timeout=12.0)
        stop_result = _compose_control(functional_harness, "stop", "jellyfin")
        assert stop_result.returncode == 0, stop_result.stderr

        release_second_item.set()
        with pytest.raises((JellyfinClientError, TimeoutError)):
            await asyncio.wait_for(sync_task, timeout=10.0)

        state = StateStore(str(tmp_path / "sync.db"))
        state.initialize()
        try:
            assert state.ping()
            assert state.count_item_maps() == 1
        finally:
            state.close()

        restart_result = _compose_control(functional_harness, "up", "-d", "jellyfin")
        assert restart_result.returncode == 0, restart_result.stderr
        _wait_for_harness_ready(client, 3)

        resumed_engine, resumed_jellyfin = _build_engine(tmp_path, functional_harness, items=items)
        result = await resumed_engine.handle_event(SyncEvent(kind="full", source="manual"))

        assert result.items_examined == 3
        assert result.errors == 0
        for item in items:
            synced = _item_by_path(resumed_jellyfin, item.path)
            assert synced.metadata.studios == (item.studio,)

        final_state = StateStore(str(tmp_path / "sync.db"))
        final_state.initialize()
        try:
            assert final_state.count_item_maps() == 3
        finally:
            final_state.close()
    finally:
        release_second_item.set()
        if restart_result is None:
            _compose_control(functional_harness, "up", "-d", "jellyfin")
        _wait_for_harness_ready(client, 3)
