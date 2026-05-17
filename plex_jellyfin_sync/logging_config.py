from __future__ import annotations

import logging
from typing import Any

import structlog

from plex_jellyfin_sync.config import AppConfig


REDACTED = "***"


def sanitize_config(config: AppConfig) -> dict[str, Any]:
    payload = config.model_dump()
    payload["plex"]["token"] = REDACTED
    payload["jellyfin"]["api_key"] = REDACTED
    payload["webhook"]["shared_secret"] = REDACTED if payload["webhook"]["shared_secret"] else None
    for mapping in payload.get("user_mapping", []):
        if mapping.get("plex_token"):
            mapping["plex_token"] = REDACTED
    return payload


def configure_logging(config: AppConfig) -> None:
    level_name = config.logging.level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(message)s", force=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    renderer: structlog.types.Processor
    if config.logging.format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
