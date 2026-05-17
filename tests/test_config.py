from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from plex_jellyfin_sync.config import load_config


def write_config(path: Path, body: str) -> None:
    path.write_text(body)


def test_load_config_applies_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
""".strip(),
    )

    config = load_config(config_path)

    assert config.sync.debounce_seconds == 900
    assert config.sync.full_sync_debounce_seconds == 30
    assert config.sync.user_data_debounce_seconds == 60
    assert config.sync.merging.refresh_timeout_seconds == 120
    assert config.sync.merging.max_requeue_count == 3


def test_load_config_supports_environment_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_TOKEN", "token-from-env")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "${PLEX_TOKEN}"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
""".strip(),
    )

    config = load_config(config_path)

    assert config.plex.token == "token-from-env"


def test_load_config_normalizes_blank_webhook_secret_to_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "   ")
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
webhook:
  shared_secret: "${WEBHOOK_SHARED_SECRET}"
""".strip(),
    )

    config = load_config(config_path)

    assert config.webhook.shared_secret is None


def test_load_config_supports_empty_default_for_optional_env_var(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
webhook:
  shared_secret: "${WEBHOOK_SHARED_SECRET:-}"
""".strip(),
    )

    config = load_config(config_path)

    assert config.webhook.shared_secret is None


def test_load_config_supports_default_state_path_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
state:
  sqlite_path: "${SYNC_SQLITE_PATH:-./state/sync.db}"
""".strip(),
    )

    config = load_config(config_path)

    assert config.state.sqlite_path == "./state/sync.db"


def test_load_config_raises_when_required_fields_missing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
""".strip(),
    )

    with pytest.raises(Exception):
        load_config(config_path)


@pytest.mark.parametrize(
    "section, field",
    [
        ("plex", "base_url"),
        ("plex", "token"),
        ("plex", "library_name"),
        ("jellyfin", "base_url"),
        ("jellyfin", "api_key"),
        ("jellyfin", "library_name"),
    ],
)
def test_load_config_raises_for_each_missing_required_field(tmp_path: Path, section: str, field: str) -> None:
    config_path = tmp_path / "config.yaml"
    body = {
        "plex": {
            "base_url": "http://plex:32400",
            "token": "plex-token",
            "library_name": "Other Video",
        },
        "jellyfin": {
            "base_url": "http://jellyfin:8096",
            "api_key": "jf-key",
            "library_name": "Other Video",
        },
    }
    del body[section][field]
    write_config(config_path, yaml.safe_dump(body))

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_raises_for_invalid_integer_field_type(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
sync:
  debounce_seconds: "fifteen"
""".strip(),
    )

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_allows_omitting_unused_jellyfin_user_id(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """
plex:
  base_url: "http://plex:32400"
  token: "plex-token"
  library_name: "Other Video"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "jf-key"
  library_name: "Other Video"
""".strip(),
    )

    config = load_config(config_path)

    assert config.jellyfin.user_id is None
