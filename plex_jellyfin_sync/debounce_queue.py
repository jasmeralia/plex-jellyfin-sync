from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress

import structlog

from plex_jellyfin_sync.models import SyncEvent


EventHandler = Callable[[SyncEvent], Awaitable[None]]


class DebounceQueue:
    def __init__(
        self,
        handler: EventHandler,
        *,
        debounce_seconds: float,
        full_sync_debounce_seconds: float,
        user_data_debounce_seconds: float = 60.0,
        max_requeue_count: int = 3,
    ) -> None:
        self._handler = handler
        self._debounce_seconds = debounce_seconds
        self._full_sync_debounce_seconds = full_sync_debounce_seconds
        self._user_data_debounce_seconds = user_data_debounce_seconds
        self._max_requeue_count = max_requeue_count
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._ready: deque[SyncEvent] = deque()
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._busy = False
        self._manual_full_sync_queued: SyncEvent | None = None
        self._logger = structlog.get_logger(__name__)

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker(), name="debounce-queue")

    async def stop(self) -> None:
        self._stop_event.set()
        self.clear_all()
        self._wake_event.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task

    def submit_item_sync(self, rating_key: int, *, requeue_count: int = 0) -> bool:
        if requeue_count >= self._max_requeue_count:
            self._logger.error("queue.drop.item", rating_key=rating_key, requeue_count=requeue_count)
            return False
        event = SyncEvent(kind="item", source="webhook", rating_key=rating_key, requeue_count=requeue_count)
        self._schedule(f"item:{rating_key}", event, self._debounce_seconds)
        return True

    def submit_user_data_sync(
        self,
        rating_key: int,
        *,
        plex_account: str,
        jellyfin_user_id: str,
        requeue_count: int = 0,
    ) -> bool:
        if requeue_count >= self._max_requeue_count:
            self._logger.error("queue.drop.userdata", rating_key=rating_key, requeue_count=requeue_count)
            return False
        key = f"userdata:{rating_key}:{plex_account}"
        event = SyncEvent(
            kind="userdata",
            source="webhook",
            rating_key=rating_key,
            plex_account=plex_account,
            jellyfin_user_id=jellyfin_user_id,
            requeue_count=requeue_count,
        )
        self._schedule(key, event, self._user_data_debounce_seconds)
        return True

    def submit_webhook_full_sync(self) -> None:
        event = SyncEvent(kind="full", source="webhook")
        self._schedule("full:webhook", event, self._full_sync_debounce_seconds)

    def submit_manual_full_sync(self, *, job_id: str | None = None) -> None:
        event = SyncEvent(kind="full", source="manual", job_id=job_id)
        self._queue_priority_full_sync(event)

    def submit_startup_full_sync(self) -> None:
        event = SyncEvent(kind="full", source="startup")
        self._queue_priority_full_sync(event)

    def _queue_priority_full_sync(self, event: SyncEvent) -> None:
        if not self._busy and not self._ready:
            self.clear_all()
            self._manual_full_sync_queued = event
        else:
            self._manual_full_sync_queued = event
        self._wake_event.set()

    def clear_all(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._ready.clear()

    async def wait_for_idle(self, *, timeout: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if not self._busy and not self._ready and not self._timers and self._manual_full_sync_queued is None:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("DebounceQueue did not become idle within timeout")
            await asyncio.sleep(0.01)

    def _schedule(self, key: str, event: SyncEvent, delay: float) -> None:
        existing = self._timers.pop(key, None)
        if existing is not None:
            existing.cancel()

        task: asyncio.Task[None] | None = None

        async def timer() -> None:
            try:
                await asyncio.sleep(delay)
                self._ready.append(event)
                self._wake_event.set()
            finally:
                if task is not None and self._timers.get(key) is task:
                    self._timers.pop(key, None)

        task = asyncio.create_task(timer(), name=f"debounce:{key}")
        self._timers[key] = task

    async def _worker(self) -> None:
        while not self._stop_event.is_set():
            await self._wake_event.wait()
            self._wake_event.clear()

            while True:
                if self._manual_full_sync_queued is not None and not self._busy:
                    event = self._manual_full_sync_queued
                    self._manual_full_sync_queued = None
                    self.clear_all()
                    await self._dispatch(event)
                    continue

                if not self._ready:
                    break

                event = self._ready.popleft()
                if self._manual_full_sync_queued is not None:
                    self._ready.appendleft(event)
                    break
                if self._should_preempt_pending_work(event):
                    self.clear_all()
                await self._dispatch(event)

    async def _dispatch(self, event: SyncEvent) -> None:
        self._busy = True
        try:
            await self._handler(event)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.exception("queue.handler_failed", error=str(exc), kind=event.kind)
        finally:
            self._busy = False
            if self._manual_full_sync_queued is not None or self._ready:
                self._wake_event.set()

    @staticmethod
    def _should_preempt_pending_work(event: SyncEvent) -> bool:
        return event.kind == "full" and event.source in {"webhook", "startup"}
