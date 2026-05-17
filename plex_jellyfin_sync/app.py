from __future__ import annotations

from contextlib import asynccontextmanager

import structlog

from plex_jellyfin_sync.config import AppConfig, load_config
from plex_jellyfin_sync.debounce_queue import DebounceQueue
from plex_jellyfin_sync.jellyfin_client import JellyfinClient
from plex_jellyfin_sync.logging_config import configure_logging, sanitize_config
from plex_jellyfin_sync.path_mapper import PathMapper
from plex_jellyfin_sync.plex_client import PlexClient
from plex_jellyfin_sync.state import StateStore
from plex_jellyfin_sync.sync_engine import SyncEngine
from plex_jellyfin_sync.webhook_server import JobTracker, create_app


def build_application(config: AppConfig):
    logger = structlog.get_logger(__name__)
    state = StateStore(config.state.sqlite_path)
    state.initialize()
    plex = PlexClient(
        base_url=config.plex.base_url,
        token=config.plex.token,
        library_name=config.plex.library_name,
        request_timeout_seconds=config.plex.request_timeout_seconds,
        max_retries=config.plex.max_retries,
        retry_backoff_seconds=config.plex.retry_backoff_seconds,
    )
    jellyfin = JellyfinClient(
        base_url=config.jellyfin.base_url,
        api_key=config.jellyfin.api_key,
        library_name=config.jellyfin.library_name,
        user_id=config.jellyfin.user_id,
        request_timeout_seconds=config.jellyfin.request_timeout_seconds,
        max_retries=config.jellyfin.max_retries,
        retry_backoff_seconds=config.jellyfin.retry_backoff_seconds,
    )
    jobs = JobTracker(jobs={})
    queue: DebounceQueue | None = None

    def requeue_event(event) -> bool:
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
        plex=plex,
        jellyfin=jellyfin,
        path_mapper=PathMapper(config.path_mapping.rules),
        requeue_callback=requeue_event,
    )

    async def handle_event(event) -> None:
        if event.job_id:
            jobs.set(event.job_id, "running")
        try:
            result = await engine.handle_event(event)
        except Exception as exc:
            if event.job_id:
                jobs.set(event.job_id, "failed", error=str(exc))
            raise
        else:
            if event.job_id:
                jobs.set(
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

    queue = DebounceQueue(
        handle_event,
        debounce_seconds=config.sync.debounce_seconds,
        full_sync_debounce_seconds=config.sync.full_sync_debounce_seconds,
        user_data_debounce_seconds=config.sync.user_data_debounce_seconds,
        max_requeue_count=config.sync.merging.max_requeue_count,
    )

    async def plex_readycheck() -> bool:
        return await plex.ready()

    async def jellyfin_readycheck() -> bool:
        return await jellyfin.ready()

    def stats_provider() -> dict:
        return {
            "items_tracked": state.count_item_maps(),
            "collections_tracked": state.count_collection_maps(),
            "last_successful_full_sync_at": state.get_last_successful_full_sync_at(),
        }

    def sync_log_provider(limit: int) -> list[dict]:
        return [
            {
                "trigger": record.trigger,
                "scope": record.scope,
                "started_at": record.started_at.isoformat(),
                "completed_at": record.completed_at.isoformat() if record.completed_at is not None else None,
                "items_examined": record.items_examined,
                "items_updated": record.items_updated,
                "user_data_updated": record.user_data_updated,
                "merges_applied": record.merges_applied,
                "unmerges_applied": record.unmerges_applied,
                "requeued_events": record.requeued_events,
                "errors": record.errors,
                "error_detail": record.error_detail,
            }
            for record in state.list_recent_sync_logs(limit)
        ]

    app = create_app(
        config=config,
        queue=queue,
        job_tracker=jobs,
        state_healthcheck=state.ping,
        plex_readycheck=plex_readycheck,
        jellyfin_readycheck=jellyfin_readycheck,
        stats_provider=stats_provider,
        sync_log_provider=sync_log_provider,
    )

    @asynccontextmanager
    async def lifespan(_app):
        logger.info(
            "app.starting",
            webhook_port=config.webhook.listen_port,
            sqlite_path=config.state.sqlite_path,
            config=sanitize_config(config),
        )
        await queue.start()
        queue.submit_startup_full_sync()
        logger.info("app.started")
        yield
        logger.info("app.stopping")
        await queue.stop()
        state.close()
        logger.info("app.stopped")

    app.router.lifespan_context = lifespan
    return app


def create_default_application(config_path: str = "/config/config.yaml"):
    config = load_config(config_path)
    configure_logging(config)
    return build_application(config)
