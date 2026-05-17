from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from plex_jellyfin_sync.config import AppConfig
from plex_jellyfin_sync.webhook_server import JobTracker, create_app


@dataclass
class FakeQueue:
    calls: list[tuple] = field(default_factory=list)

    def submit_item_sync(self, rating_key: int, *, requeue_count: int = 0) -> bool:
        self.calls.append(("item", rating_key, requeue_count))
        return True

    def submit_user_data_sync(
        self,
        rating_key: int,
        *,
        plex_account: str,
        jellyfin_user_id: str,
        requeue_count: int = 0,
    ) -> bool:
        self.calls.append(("userdata", rating_key, plex_account, jellyfin_user_id, requeue_count))
        return True

    def submit_webhook_full_sync(self) -> None:
        self.calls.append(("webhook-full",))

    def submit_manual_full_sync(self, *, job_id: str | None = None) -> None:
        self.calls.append(("manual-full", job_id))


def build_config() -> AppConfig:
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
                    "jellyfin_user_id": "jf-user-1",
                }
            ],
            "webhook": {"shared_secret": "secret"},
        }
    )


@pytest.mark.asyncio
async def test_webhook_library_new_enqueues_item_sync() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            json={"event": "library.new", "Metadata": {"ratingKey": "42"}},
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 200
    assert queue.calls == [("item", 42, 0)]


@pytest.mark.asyncio
async def test_webhook_accepts_urlencoded_payload() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            data={"payload": json.dumps({"event": "library.new", "Metadata": {"ratingKey": "42"}})},
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 200
    assert queue.calls == [("item", 42, 0)]


@pytest.mark.asyncio
async def test_webhook_accepts_multipart_payload() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            files={
                "payload": (None, json.dumps({"event": "library.new", "Metadata": {"ratingKey": "42"}})),
                "thumb": ("thumb.jpg", b"jpeg-bytes", "image/jpeg"),
            },
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 200
    assert queue.calls == [("item", 42, 0)]


@pytest.mark.asyncio
async def test_webhook_scrobble_for_mapped_account_enqueues_user_data_sync() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            json={
                "event": "media.scrobble",
                "Metadata": {"ratingKey": "42"},
                "Account": {"title": "jas"},
            },
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 200
    assert queue.calls == [("userdata", 42, "jas", "jf-user-1", 0)]


@pytest.mark.asyncio
async def test_webhook_scrobble_for_unmapped_account_is_ignored() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            json={
                "event": "media.scrobble",
                "Metadata": {"ratingKey": "42"},
                "Account": {"title": "someone-else"},
            },
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert queue.calls == []


@pytest.mark.asyncio
async def test_webhook_media_rate_for_mapped_account_enqueues_user_data_sync() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            json={
                "event": "media.rate",
                "Metadata": {"ratingKey": "42"},
                "Account": {"title": "jas"},
            },
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 200
    assert queue.calls == [("userdata", 42, "jas", "jf-user-1", 0)]


@pytest.mark.asyncio
async def test_webhook_media_play_is_ignored() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            json={
                "event": "media.play",
                "Metadata": {"ratingKey": "42"},
                "Account": {"title": "jas"},
            },
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert queue.calls == []


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_shared_secret() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/webhook/plex", json={"event": "library.new", "Metadata": {"ratingKey": "42"}})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_allows_requests_when_shared_secret_is_blank() -> None:
    queue = FakeQueue()
    config = AppConfig.model_validate(
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
                    "jellyfin_user_id": "jf-user-1",
                }
            ],
            "webhook": {"shared_secret": "   "},
        }
    )
    app = create_app(
        config=config,
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            json={"event": "library.new", "Metadata": {"ratingKey": "42"}},
        )

    assert response.status_code == 200
    assert queue.calls == [("item", 42, 0)]


@pytest.mark.asyncio
async def test_webhook_returns_400_for_malformed_json() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            content="{",
            headers={"content-type": "application/json", "x-webhook-secret": "secret"},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_webhook_returns_400_for_invalid_rating_key() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/plex",
            json={"event": "library.new", "Metadata": {"ratingKey": "not-an-int"}},
            headers={"x-webhook-secret": "secret"},
        )

    assert response.status_code == 400
    assert queue.calls == []


@pytest.mark.asyncio
async def test_manual_full_sync_trigger_returns_job_id_and_queue_status() -> None:
    queue = FakeQueue()
    tracker = JobTracker(jobs={})
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=tracker,
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/trigger/full-sync")

        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert response.json()["created_at"]
        assert queue.calls == [("manual-full", job_id)]

        status_response = await client.get(f"/trigger/status/{job_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "queued"
    assert status_response.json()["result"] is None
    assert status_response.json()["error"] is None


@pytest.mark.asyncio
async def test_trigger_status_returns_result_details() -> None:
    queue = FakeQueue()
    tracker = JobTracker(jobs={})
    job_id = tracker.create()
    tracker.set(job_id, "complete", result={"items_updated": 2})
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=tracker,
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"/trigger/status/{job_id}")

    assert response.status_code == 200
    assert response.json()["result"] == {"items_updated": 2}


@pytest.mark.asyncio
async def test_trigger_status_returns_404_for_unknown_job() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_true_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/trigger/status/does-not-exist")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints() -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: False,
        plex_readycheck=_true_async,
        jellyfin_readycheck=_false_async,
        stats_provider=lambda: {"items_tracked": 2, "collections_tracked": 1, "last_successful_full_sync_at": None},
        sync_log_provider=lambda limit: [{"scope": "full", "limit": limit}],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health_response = await client.get("/healthz")
        ready_response = await client.get("/readyz")
        stats_response = await client.get("/admin/stats")
        sync_log_response = await client.get("/admin/sync-log?limit=9999")

    assert health_response.status_code == 503
    assert ready_response.status_code == 503
    assert stats_response.status_code == 200
    assert stats_response.json()["items_tracked"] == 2
    assert sync_log_response.status_code == 200
    assert sync_log_response.json() == [{"scope": "full", "limit": 500}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plex_ok", "jellyfin_ok"),
    [
        (False, True),
        (True, False),
    ],
)
async def test_readyz_returns_503_when_any_dependency_is_unavailable(plex_ok: bool, jellyfin_ok: bool) -> None:
    queue = FakeQueue()
    app = create_app(
        config=build_config(),
        queue=queue,  # type: ignore[arg-type]
        job_tracker=JobTracker(jobs={}),
        state_healthcheck=lambda: True,
        plex_readycheck=_true_async if plex_ok else _false_async,
        jellyfin_readycheck=_true_async if jellyfin_ok else _false_async,
        stats_provider=lambda: {},
        sync_log_provider=lambda _limit: [],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503


async def _true_async() -> bool:
    return True


async def _false_async() -> bool:
    return False
