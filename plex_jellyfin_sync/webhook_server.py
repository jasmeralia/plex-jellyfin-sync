from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response, status

from plex_jellyfin_sync.config import AppConfig
from plex_jellyfin_sync.debounce_queue import DebounceQueue


Healthcheck = Callable[[], bool]
AsyncHealthcheck = Callable[[], Awaitable[bool]]
StatsProvider = Callable[[], dict]
SyncLogProvider = Callable[[int], list[dict]]


@dataclass
class JobRecord:
    job_id: str
    status: str
    created_at: str
    updated_at: str
    result: dict | None = None
    error: str | None = None


@dataclass
class JobTracker:
    jobs: dict[str, JobRecord]

    def create(self) -> str:
        job_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat()
        self.jobs[job_id] = JobRecord(
            job_id=job_id,
            status="queued",
            created_at=timestamp,
            updated_at=timestamp,
        )
        return job_id

    def set(self, job_id: str, state: str, *, result: dict | None = None, error: str | None = None) -> None:
        if job_id in self.jobs:
            record = self.jobs[job_id]
            record.status = state
            record.updated_at = datetime.now(UTC).isoformat()
            if result is not None:
                record.result = result
            if error is not None:
                record.error = error

    def get(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)


def _account_name(payload: dict) -> str | None:
    account = payload.get("Account")
    if isinstance(account, dict):
        return account.get("title") or account.get("name")
    if isinstance(account, str):
        return account
    return None


async def _load_payload(request: Request) -> dict:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload") from exc
    raw_payload = _extract_form_payload(content_type, await request.body())
    if not raw_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing payload")
    try:
        return json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed payload") from exc


def _extract_form_payload(content_type: str, body: bytes) -> str | None:
    if "application/x-www-form-urlencoded" in content_type:
        payloads = parse_qs(body.decode("utf-8"), keep_blank_values=True).get("payload")
        return payloads[0] if payloads else None
    if "multipart/form-data" not in content_type:
        return None

    parser = BytesParser(policy=default)
    message = parser.parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    if not message.is_multipart():
        return None

    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") != "payload":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            return None
        charset = part.get_content_charset("utf-8")
        return payload.decode(charset)
    return None


def create_app(
    *,
    config: AppConfig,
    queue: DebounceQueue,
    job_tracker: JobTracker,
    state_healthcheck: Healthcheck,
    plex_readycheck: AsyncHealthcheck,
    jellyfin_readycheck: AsyncHealthcheck,
    stats_provider: StatsProvider,
    sync_log_provider: SyncLogProvider,
) -> FastAPI:
    app = FastAPI()
    user_mapping = {entry.plex_account: entry.jellyfin_user_id for entry in config.user_mapping}

    @app.get("/healthz")
    async def healthz() -> Response:
        return Response(status_code=status.HTTP_200_OK if state_healthcheck() else status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.get("/readyz")
    async def readyz() -> Response:
        state_ok = state_healthcheck()
        plex_ok = await plex_readycheck()
        jellyfin_ok = await jellyfin_readycheck()
        code = status.HTTP_200_OK if state_ok and plex_ok and jellyfin_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(status_code=code)

    @app.get("/admin/stats")
    async def admin_stats() -> dict:
        return stats_provider()

    @app.get("/admin/sync-log")
    async def admin_sync_log(limit: int = 50) -> list[dict]:
        bounded_limit = max(1, min(limit, 500))
        return sync_log_provider(bounded_limit)

    @app.post("/trigger/full-sync", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_full_sync() -> dict[str, str]:
        job_id = job_tracker.create()
        queue.submit_manual_full_sync(job_id=job_id)
        record = job_tracker.get(job_id)
        return {"job_id": job_id, "status": "queued", "created_at": record.created_at if record else ""}

    @app.get("/trigger/status/{job_id}")
    async def trigger_status(job_id: str) -> dict:
        record = job_tracker.get(job_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job")
        return {
            "job_id": record.job_id,
            "status": record.status,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "result": record.result,
            "error": record.error,
        }

    @app.post("/webhook/plex")
    async def plex_webhook(request: Request) -> dict[str, str]:
        shared_secret = config.webhook.shared_secret
        if shared_secret is not None:
            provided = request.headers.get("x-webhook-secret")
            if provided != shared_secret:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid secret")

        payload = await _load_payload(request)
        event = payload.get("event")
        metadata = payload.get("Metadata") or {}
        rating_key = metadata.get("ratingKey")
        if rating_key is not None:
            try:
                rating_key = int(rating_key)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ratingKey") from exc

        if event == "library.new":
            if rating_key is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing ratingKey")
            queue.submit_item_sync(rating_key)
            return {"status": "queued"}

        if event in {"media.scrobble", "media.rate"}:
            if rating_key is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing ratingKey")
            account_name = _account_name(payload)
            jellyfin_user_id = user_mapping.get(account_name or "")
            if jellyfin_user_id is None:
                return {"status": "ignored"}
            queue.submit_user_data_sync(
                rating_key,
                plex_account=account_name or "",
                jellyfin_user_id=jellyfin_user_id,
            )
            return {"status": "queued"}

        return {"status": "ignored"}

    return app
