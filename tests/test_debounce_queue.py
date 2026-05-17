from __future__ import annotations

import asyncio

import pytest

from plex_jellyfin_sync.debounce_queue import DebounceQueue
from plex_jellyfin_sync.models import SyncEvent


@pytest.mark.asyncio
async def test_debounce_queue_dispatches_single_item_after_window() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.02, full_sync_debounce_seconds=0.01, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_item_sync(123)
    await asyncio.sleep(0.05)
    await queue.wait_for_idle()
    await queue.stop()

    assert [event.kind for event in dispatched] == ["item"]
    assert dispatched[0].rating_key == 123


@pytest.mark.asyncio
async def test_debounce_queue_resets_window_for_same_key() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.03, full_sync_debounce_seconds=0.01, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_item_sync(123)
    await asyncio.sleep(0.02)
    queue.submit_item_sync(123)
    await asyncio.sleep(0.02)
    assert dispatched == []
    await asyncio.sleep(0.03)
    await queue.wait_for_idle()
    await queue.stop()

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_manual_full_sync_preempts_pending_work_when_idle() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.2, full_sync_debounce_seconds=0.1, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_item_sync(123)
    queue.submit_manual_full_sync(job_id="job-1")
    await asyncio.sleep(0.05)
    await queue.wait_for_idle()
    await queue.stop()

    assert [(event.kind, event.source, event.job_id) for event in dispatched] == [("full", "manual", "job-1")]


@pytest.mark.asyncio
async def test_manual_full_sync_coalesces_when_already_queued() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.2, full_sync_debounce_seconds=0.1, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_manual_full_sync(job_id="job-1")
    queue.submit_manual_full_sync(job_id="job-2")
    await asyncio.sleep(0.05)
    await queue.wait_for_idle()
    await queue.stop()

    assert [(event.kind, event.source, event.job_id) for event in dispatched] == [("full", "manual", "job-2")]


@pytest.mark.asyncio
async def test_startup_full_sync_dispatches_immediately_when_idle() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.2, full_sync_debounce_seconds=0.1, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_item_sync(123)
    queue.submit_startup_full_sync()
    await asyncio.sleep(0.05)
    await queue.wait_for_idle()
    await queue.stop()

    assert [(event.kind, event.source, event.job_id) for event in dispatched] == [("full", "startup", None)]


@pytest.mark.asyncio
async def test_queue_survives_handler_exception() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)
        if event.rating_key == 1:
            raise RuntimeError("boom")

    queue = DebounceQueue(handler, debounce_seconds=0.01, full_sync_debounce_seconds=0.01, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_item_sync(1)
    queue.submit_item_sync(2)
    await asyncio.sleep(0.05)
    await queue.wait_for_idle()
    await queue.stop()

    assert [event.rating_key for event in dispatched] == [1, 2]


@pytest.mark.asyncio
async def test_webhook_full_sync_coalesces_into_single_dispatch() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.1, full_sync_debounce_seconds=0.02, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_webhook_full_sync()
    await asyncio.sleep(0.01)
    queue.submit_webhook_full_sync()
    await asyncio.sleep(0.04)
    await queue.wait_for_idle()
    await queue.stop()

    assert [(event.kind, event.source) for event in dispatched] == [("full", "webhook")]


@pytest.mark.asyncio
async def test_webhook_full_sync_preempts_pending_item_and_user_data_work() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.2, full_sync_debounce_seconds=0.02, user_data_debounce_seconds=0.2)
    await queue.start()
    queue.submit_item_sync(123)
    queue.submit_user_data_sync(123, plex_account="jas", jellyfin_user_id="jf-user-1")
    queue.submit_webhook_full_sync()
    await asyncio.sleep(0.06)
    await queue.wait_for_idle()
    await queue.stop()

    assert [(event.kind, event.source, event.rating_key) for event in dispatched] == [("full", "webhook", None)]


@pytest.mark.asyncio
async def test_manual_full_sync_queues_single_follow_up_when_busy_and_clears_pending_work() -> None:
    dispatched: list[SyncEvent] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)
        if event.kind == "item":
            started.set()
            await release.wait()

    queue = DebounceQueue(handler, debounce_seconds=0.01, full_sync_debounce_seconds=0.01, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_item_sync(123)
    await started.wait()
    queue.submit_item_sync(456)
    queue.submit_manual_full_sync(job_id="job-1")
    queue.submit_manual_full_sync(job_id="job-2")
    release.set()
    await asyncio.sleep(0.05)
    await queue.wait_for_idle()
    await queue.stop()

    assert [(event.kind, event.source, event.rating_key, event.job_id) for event in dispatched] == [
        ("item", "webhook", 123, None),
        ("full", "manual", None, "job-2"),
    ]


@pytest.mark.asyncio
async def test_item_and_user_data_events_for_same_rating_key_use_independent_windows() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.01, full_sync_debounce_seconds=0.05, user_data_debounce_seconds=0.01)
    await queue.start()
    queue.submit_item_sync(123)
    queue.submit_user_data_sync(123, plex_account="jas", jellyfin_user_id="jf-user-1")
    await asyncio.sleep(0.05)
    await queue.wait_for_idle()
    await queue.stop()

    assert [(event.kind, event.rating_key, event.plex_account) for event in dispatched] == [
        ("item", 123, None),
        ("userdata", 123, "jas"),
    ]


@pytest.mark.asyncio
async def test_user_data_window_resets_for_same_item_and_account() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(handler, debounce_seconds=0.05, full_sync_debounce_seconds=0.05, user_data_debounce_seconds=0.03)
    await queue.start()
    queue.submit_user_data_sync(123, plex_account="jas", jellyfin_user_id="jf-user-1")
    await asyncio.sleep(0.02)
    queue.submit_user_data_sync(123, plex_account="jas", jellyfin_user_id="jf-user-1", requeue_count=1)
    await asyncio.sleep(0.02)
    assert dispatched == []
    await asyncio.sleep(0.03)
    await queue.wait_for_idle()
    await queue.stop()

    assert len(dispatched) == 1
    assert dispatched[0].requeue_count == 1


@pytest.mark.asyncio
async def test_item_requeue_restarts_window_and_max_requeue_is_dropped() -> None:
    dispatched: list[SyncEvent] = []

    async def handler(event: SyncEvent) -> None:
        dispatched.append(event)

    queue = DebounceQueue(
        handler,
        debounce_seconds=0.03,
        full_sync_debounce_seconds=0.05,
        user_data_debounce_seconds=0.05,
        max_requeue_count=2,
    )
    await queue.start()
    assert queue.submit_item_sync(123, requeue_count=1) is True
    await asyncio.sleep(0.02)
    assert queue.submit_item_sync(123, requeue_count=2) is False
    await asyncio.sleep(0.02)
    await queue.wait_for_idle()
    await queue.stop()

    assert len(dispatched) == 1
    assert dispatched[0].requeue_count == 1
