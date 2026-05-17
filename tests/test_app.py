from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

import plex_jellyfin_sync.app as app_module
from plex_jellyfin_sync.app import build_application
from plex_jellyfin_sync.config import AppConfig
from plex_jellyfin_sync.models import CollectionMapRecord, ItemMapRecord, SyncLogRecord
from plex_jellyfin_sync.state import StateStore


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
            "state": {"sqlite_path": str(tmp_path / "sync.db")},
        }
    )


@pytest.mark.asyncio
async def test_admin_stats_reports_state_counts(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    state.upsert_item_map(
        ItemMapRecord(
            plex_rating_key=1,
            jellyfin_primary_id="item-1",
            is_merged=False,
            content_hash="hash",
            last_synced_at=now,
        )
    )
    state.upsert_collection_map(
        CollectionMapRecord(
            plex_collection_key=10,
            jellyfin_id="box-1",
            name="Favorites",
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
            errors=0,
        ),
    )
    state.close()

    app = build_application(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin/stats")

    assert response.status_code == 200
    assert response.json()["items_tracked"] == 1
    assert response.json()["collections_tracked"] == 1
    assert response.json()["last_successful_full_sync_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_admin_sync_log_reports_recent_runs(tmp_path) -> None:
    config = build_config(tmp_path)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    log_id = state.create_sync_log(SyncLogRecord(trigger="manual", scope="full", started_at=now))
    state.update_sync_log(
        log_id,
        SyncLogRecord(
            trigger="manual",
            scope="full",
            started_at=now,
            completed_at=now,
            items_examined=3,
            items_updated=2,
            errors=0,
        ),
    )
    state.close()

    app = build_application(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin/sync-log?limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["scope"] == "full"
    assert payload[0]["items_examined"] == 3


@pytest.mark.asyncio
async def test_application_lifespan_enqueues_startup_full_sync(monkeypatch, tmp_path) -> None:
    config = build_config(tmp_path)
    calls: list[str] = []

    class FakeQueue:
        def __init__(self, *args, **kwargs) -> None:
            self.started = False

        async def start(self) -> None:
            self.started = True
            calls.append("start")

        def submit_startup_full_sync(self) -> None:
            assert self.started is True
            calls.append("startup")

        async def stop(self) -> None:
            calls.append("stop")

        def submit_item_sync(self, rating_key: int, *, requeue_count: int = 0) -> bool:
            return True

        def submit_user_data_sync(
            self,
            rating_key: int,
            *,
            plex_account: str,
            jellyfin_user_id: str,
            requeue_count: int = 0,
        ) -> bool:
            return True

        def submit_webhook_full_sync(self) -> None:
            return None

        def submit_manual_full_sync(self, *, job_id: str | None = None) -> None:
            return None

    class FakePlexClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def ready(self) -> bool:
            return True

    class FakeJellyfinClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def ready(self) -> bool:
            return True

    monkeypatch.setattr(app_module, "DebounceQueue", FakeQueue)
    monkeypatch.setattr(app_module, "PlexClient", FakePlexClient)
    monkeypatch.setattr(app_module, "JellyfinClient", FakeJellyfinClient)

    app = build_application(config)

    async with app.router.lifespan_context(app):
        assert calls == ["start", "startup"]

    assert calls == ["start", "startup", "stop"]
