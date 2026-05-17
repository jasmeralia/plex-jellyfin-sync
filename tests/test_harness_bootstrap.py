from __future__ import annotations

from pathlib import Path

import pytest

from plex_jellyfin_sync.harness_bootstrap import (
    HarnessBootstrapError,
    ensure_admin_session,
    ensure_library,
    ensure_api_key,
    update_harness_config,
    write_env_file,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        if payload is None:
            self.content = b""
        else:
            self.content = b"json"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "params": params or {},
                "json": json,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def test_ensure_api_key_reuses_active_existing_key() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                [
                    {"AppName": "other-app", "AccessToken": "old"},
                    {"AppName": "plex-jellyfin-sync", "AccessToken": "existing-key"},
                ],
            )
        ]
    )

    api_key = ensure_api_key(session, "http://jellyfin:8096", admin_token="admin-token", app_name="plex-jellyfin-sync")

    assert api_key == "existing-key"
    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "GET"


def test_ensure_library_reuses_existing_virtual_folder() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                [
                    {"Name": "Movies"},
                    {"Name": "Other Video", "Locations": ["/media/othervideo"]},
                ],
            )
        ]
    )

    ensure_library(
        session,
        "http://jellyfin:8096",
        admin_token="admin-token",
        library_name="Other Video",
        media_path="/media/othervideo",
    )

    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "GET"


def test_ensure_library_creates_virtual_folder_when_missing() -> None:
    session = FakeSession(
        [
            FakeResponse(200, [{"Name": "Movies"}]),
            FakeResponse(204),
        ]
    )

    ensure_library(
        session,
        "http://jellyfin:8096",
        admin_token="admin-token",
        library_name="Other Video",
        media_path="/media/othervideo",
    )

    assert session.calls[1]["method"] == "POST"
    assert session.calls[1]["url"].endswith("/Library/VirtualFolders")
    assert session.calls[1]["params"] == {
        "name": "Other Video",
        "collectionType": "mixed",
        "paths": ["/media/othervideo"],
        "refreshLibrary": "true",
    }


def test_ensure_api_key_creates_key_when_missing() -> None:
    session = FakeSession(
        [
            FakeResponse(200, []),
            FakeResponse(204),
            FakeResponse(200, [{"AppName": "plex-jellyfin-sync", "AccessToken": "new-key"}]),
        ]
    )

    api_key = ensure_api_key(session, "http://jellyfin:8096", admin_token="admin-token", app_name="plex-jellyfin-sync")

    assert api_key == "new-key"
    assert session.calls[1]["method"] == "POST"
    assert session.calls[1]["params"] == {"app": "plex-jellyfin-sync"}


def test_ensure_admin_session_bootstraps_on_initial_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(
        [
            FakeResponse(200, {"ServerName": "Harness"}),
            FakeResponse(401, text="unauthorized"),
            FakeResponse(204),
            FakeResponse(200, {"Name": "root"}),
            FakeResponse(204),
            FakeResponse(204),
            FakeResponse(204),
            FakeResponse(200, {"AccessToken": "admin-token", "User": {"Id": "admin-id"}}),
        ]
    )
    monkeypatch.setattr("plex_jellyfin_sync.harness_bootstrap.time.sleep", lambda _: None)

    token, user_id, username = ensure_admin_session(
        session,
        "http://jellyfin:8096",
        username="admin",
        password="secret",
        server_name="Harness",
        timeout_seconds=0.1,
    )

    assert token == "admin-token"
    assert user_id == "admin-id"
    assert username == "root"
    assert [call["url"].split("8096", 1)[1] for call in session.calls[2:7]] == [
        "/Startup/Configuration",
        "/Startup/User",
        "/Startup/User",
        "/Startup/RemoteAccess",
        "/Startup/Complete",
    ]
    assert session.calls[4]["json"] == {"Name": "root", "Password": "secret"}


def test_ensure_admin_session_raises_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([])
    state = {"now": 0.0}

    def fake_monotonic() -> float:
        state["now"] += 1.0
        return state["now"]

    monkeypatch.setattr("plex_jellyfin_sync.harness_bootstrap.time.monotonic", fake_monotonic)
    monkeypatch.setattr("plex_jellyfin_sync.harness_bootstrap.time.sleep", lambda _: None)

    with pytest.raises(HarnessBootstrapError):
        ensure_admin_session(
            session,
            "http://jellyfin:8096",
            username="admin",
            password="secret",
            server_name="Harness",
            timeout_seconds=0.5,
        )


def test_update_harness_config_sets_user_id(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
plex:
  base_url: "http://plex:32400"
jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "${JELLYFIN_API_KEY}"
""".strip(),
        encoding="utf-8",
    )

    update_harness_config(config_path, user_id="admin-id")

    contents = config_path.read_text(encoding="utf-8")
    assert 'user_id: admin-id' in contents
    assert 'api_key: ${JELLYFIN_API_KEY}' in contents


def test_write_env_file_shell_quotes_values(tmp_path: Path) -> None:
    env_path = tmp_path / "bootstrap.env"

    write_env_file(env_path, {"JELLYFIN_API_KEY": "abc123", "JF_BOOTSTRAP_ADMIN_USERNAME": "admin user"})

    assert env_path.read_text(encoding="utf-8") == "JELLYFIN_API_KEY=abc123\nJF_BOOTSTRAP_ADMIN_USERNAME='admin user'\n"
