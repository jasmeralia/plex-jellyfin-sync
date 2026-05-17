from plex_jellyfin_sync.config import AppConfig
from plex_jellyfin_sync.logging_config import REDACTED, sanitize_config


def test_sanitize_config_redacts_secrets() -> None:
    config = AppConfig.model_validate(
        {
            "plex": {
                "base_url": "http://plex:32400",
                "token": "plex-secret",
                "library_name": "Other Video",
            },
            "jellyfin": {
                "base_url": "http://jellyfin:8096",
                "api_key": "jf-secret",
                "user_id": "admin-id",
                "library_name": "Other Video",
            },
            "user_mapping": [
                {
                    "plex_account": "jas",
                    "plex_token": "user-secret",
                    "jellyfin_user_id": "jf-user-1",
                }
            ],
            "webhook": {"shared_secret": "hook-secret"},
        }
    )

    sanitized = sanitize_config(config)

    assert sanitized["plex"]["token"] == REDACTED
    assert sanitized["jellyfin"]["api_key"] == REDACTED
    assert sanitized["webhook"]["shared_secret"] == REDACTED
    assert sanitized["user_mapping"][0]["plex_token"] == REDACTED
