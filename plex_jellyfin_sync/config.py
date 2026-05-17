from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class PlexConfig(BaseModel):
    base_url: str
    token: str
    library_name: str
    request_timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5


class JellyfinConfig(BaseModel):
    base_url: str
    api_key: str
    user_id: str | None = None
    library_name: str
    request_timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5


class UserMappingEntry(BaseModel):
    plex_account: str
    jellyfin_user_id: str
    plex_token: str | None = None


class PathMappingRule(BaseModel):
    plex_prefix: str
    jellyfin_prefix: str


class PathMappingConfig(BaseModel):
    rules: list[PathMappingRule] = Field(default_factory=list)


class FieldMappingConfig(BaseModel):
    studio: bool = True
    writers_as_actors: bool = True
    directors: bool = True
    collections: bool = True


class UserDataConfig(BaseModel):
    watched: bool = True
    play_count: bool = True
    rating: bool = True


class MergingConfig(BaseModel):
    enabled: bool = True
    refresh_timeout_seconds: int = 120
    max_requeue_count: int = 3


class SyncConfig(BaseModel):
    debounce_seconds: int = 900
    full_sync_debounce_seconds: int = 30
    user_data_debounce_seconds: int = 60
    field_mapping: FieldMappingConfig = Field(default_factory=FieldMappingConfig)
    user_data: UserDataConfig = Field(default_factory=UserDataConfig)
    merging: MergingConfig = Field(default_factory=MergingConfig)
    lock_synced_fields: bool = True


class WebhookConfig(BaseModel):
    listen_port: int = 8089
    shared_secret: str | None = None

    @field_validator("shared_secret", mode="before")
    @classmethod
    def normalize_shared_secret(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"


class StateConfig(BaseModel):
    sqlite_path: str = "/state/sync.db"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plex: PlexConfig
    jellyfin: JellyfinConfig
    user_mapping: list[UserMappingEntry] = Field(default_factory=list)
    path_mapping: PathMappingConfig = Field(default_factory=PathMappingConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    state: StateConfig = Field(default_factory=StateConfig)

    @field_validator("plex", "jellyfin")
    @classmethod
    def validate_required_strings(cls, value: BaseModel) -> BaseModel:
        for name, item in value.model_dump().items():
            if isinstance(item, str) and not item:
                raise ValueError(f"{value.__class__.__name__}.{name} must not be empty")
        return value


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2)
            if name not in os.environ:
                if default is not None:
                    return default
                raise ValueError(f"Missing environment variable: {name}")
            return os.environ[name]

        return ENV_PATTERN.sub(replace, value)
    return value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text()) or {}
    expanded = _expand_env(raw)
    return AppConfig.model_validate(expanded)
