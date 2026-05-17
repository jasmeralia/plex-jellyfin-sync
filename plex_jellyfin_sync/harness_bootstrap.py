from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
import shlex
import time
from typing import Any

import requests
import yaml


DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "plex-jellyfin-sync"
DEFAULT_SERVER_NAME = "plex-jellyfin-sync-harness"
DEFAULT_APP_NAME = "plex-jellyfin-sync"
DEFAULT_DEVICE_ID = "plex-jellyfin-sync-harness"
DEFAULT_CLIENT_NAME = "plex-jellyfin-sync-harness"
DEFAULT_DEVICE_NAME = "Codex Smoke Harness"
DEFAULT_CLIENT_VERSION = "0.1.0"


class HarnessBootstrapError(RuntimeError):
    """Raised when the local Jellyfin harness bootstrap fails."""


def media_browser_authorization_header(token: str | None = None) -> str:
    parts = [
        f'Client="{DEFAULT_CLIENT_NAME}"',
        f'Device="{DEFAULT_DEVICE_NAME}"',
        f'DeviceId="{DEFAULT_DEVICE_ID}"',
        f'Version="{DEFAULT_CLIENT_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


def _request_json(
    session: requests.Session,
    method: str,
    base_url: str,
    path: str,
    *,
    token: str | None = None,
    params: Mapping[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    headers = {"X-Emby-Authorization": media_browser_authorization_header(token)}
    if token:
        headers["X-Emby-Token"] = token
    response = session.request(
        method,
        f"{base_url.rstrip('/')}{path}",
        headers=headers,
        params=dict(params or {}),
        json=json_body,
        timeout=10,
    )
    if response.status_code >= 400:
        raise HarnessBootstrapError(f"{method} {path} failed with status {response.status_code}: {response.text.strip()}")
    if not response.content:
        return None
    return response.json()


def wait_for_server(session: requests.Session, base_url: str, *, timeout_seconds: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _request_json(session, "GET", base_url, "/System/Info/Public")
            return
        except Exception as exc:  # pragma: no cover - exercised via timeout branch
            last_error = exc
            time.sleep(2)
    if last_error is not None:
        raise HarnessBootstrapError(f"Timed out waiting for Jellyfin at {base_url}: {last_error}") from last_error
    raise HarnessBootstrapError(f"Timed out waiting for Jellyfin at {base_url}")


def authenticate(session: requests.Session, base_url: str, *, username: str, password: str) -> tuple[str, str]:
    payload = _request_json(
        session,
        "POST",
        base_url,
        "/Users/AuthenticateByName",
        json_body={"Username": username, "Pw": password},
    )
    access_token = payload.get("AccessToken")
    user = payload.get("User") or {}
    user_id = user.get("Id") or payload.get("UserId")
    if not access_token or not user_id:
        raise HarnessBootstrapError("Jellyfin authentication response did not include both AccessToken and User.Id")
    return str(access_token), str(user_id)


def bootstrap_startup_wizard(
    session: requests.Session,
    base_url: str,
    *,
    username: str,
    password: str,
    server_name: str,
) -> str:
    _request_json(
        session,
        "POST",
        base_url,
        "/Startup/Configuration",
        json_body={
            "UICulture": "en-US",
            "MetadataCountryCode": "US",
            "PreferredMetadataLanguage": "en",
            "ServerName": server_name,
        },
    )
    startup_user = _request_json(session, "GET", base_url, "/Startup/User")
    startup_username = str(startup_user.get("Name") or username) if isinstance(startup_user, dict) else username
    _request_json(
        session,
        "POST",
        base_url,
        "/Startup/User",
        json_body={"Name": startup_username, "Password": password},
    )
    _request_json(
        session,
        "POST",
        base_url,
        "/Startup/RemoteAccess",
        json_body={"EnableRemoteAccess": False, "EnableAutomaticPortMapping": False},
    )
    _request_json(session, "POST", base_url, "/Startup/Complete")
    return startup_username


def ensure_admin_session(
    session: requests.Session,
    base_url: str,
    *,
    username: str,
    password: str,
    server_name: str,
    timeout_seconds: float = 120.0,
) -> tuple[str, str, str]:
    wait_for_server(session, base_url, timeout_seconds=timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            token, user_id = authenticate(session, base_url, username=username, password=password)
            return token, user_id, username
        except HarnessBootstrapError as exc:
            last_error = exc

        try:
            startup_username = bootstrap_startup_wizard(
                session,
                base_url,
                username=username,
                password=password,
                server_name=server_name,
            )
        except HarnessBootstrapError as exc:
            last_error = exc
            time.sleep(2)
            continue

        try:
            token, user_id = authenticate(session, base_url, username=startup_username, password=password)
            return token, user_id, startup_username
        except HarnessBootstrapError as exc:
            last_error = exc
            time.sleep(2)

    if last_error is not None:
        raise HarnessBootstrapError(f"Timed out establishing Jellyfin admin session at {base_url}: {last_error}") from last_error
    raise HarnessBootstrapError(f"Timed out establishing Jellyfin admin session at {base_url}")


def _coerce_auth_info_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("Items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _coerce_virtual_folders(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("Items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def ensure_library(
    session: requests.Session,
    base_url: str,
    *,
    admin_token: str,
    library_name: str,
    media_path: str,
    collection_type: str = "mixed",
) -> None:
    folders = _coerce_virtual_folders(_request_json(session, "GET", base_url, "/Library/VirtualFolders", token=admin_token))
    for folder in folders:
        if folder.get("Name") == library_name:
            return

    _request_json(
        session,
        "POST",
        base_url,
        "/Library/VirtualFolders",
        token=admin_token,
        params={
            "name": library_name,
            "collectionType": collection_type,
            "paths": [media_path],
            "refreshLibrary": "true",
        },
    )


def ensure_api_key(session: requests.Session, base_url: str, *, admin_token: str, app_name: str) -> str:
    keys = _coerce_auth_info_items(_request_json(session, "GET", base_url, "/Auth/Keys", token=admin_token))
    for key in keys:
        if key.get("AppName") == app_name and key.get("AccessToken") and not key.get("DateRevoked"):
            return str(key["AccessToken"])

    _request_json(
        session,
        "POST",
        base_url,
        "/Auth/Keys",
        token=admin_token,
        params={"app": app_name},
    )
    keys = _coerce_auth_info_items(_request_json(session, "GET", base_url, "/Auth/Keys", token=admin_token))
    for key in keys:
        if key.get("AppName") == app_name and key.get("AccessToken") and not key.get("DateRevoked"):
            return str(key["AccessToken"])
    raise HarnessBootstrapError(f"Created API key for app {app_name!r}, but could not resolve it from /Auth/Keys")


def update_harness_config(config_path: Path, *, user_id: str) -> None:
    document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    jellyfin = document.setdefault("jellyfin", {})
    jellyfin["user_id"] = user_id
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def write_env_file(path: Path, values: Mapping[str, str]) -> None:
    lines = [f"{key}={shlex.quote(value)}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bootstrap_harness(
    *,
    base_url: str,
    config_path: Path,
    env_file: Path,
    username: str,
    password: str,
    server_name: str,
    app_name: str,
    timeout_seconds: float,
    library_name: str,
    media_path: str,
    collection_type: str = "mixed",
) -> tuple[str, str]:
    session = requests.Session()
    admin_token, user_id, admin_username = ensure_admin_session(
        session,
        base_url,
        username=username,
        password=password,
        server_name=server_name,
        timeout_seconds=timeout_seconds,
    )
    ensure_library(
        session,
        base_url,
        admin_token=admin_token,
        library_name=library_name,
        media_path=media_path,
        collection_type=collection_type,
    )
    api_key = ensure_api_key(session, base_url, admin_token=admin_token, app_name=app_name)
    update_harness_config(config_path, user_id=user_id)
    write_env_file(
        env_file,
        {
            "JELLYFIN_API_KEY": api_key,
            "JELLYFIN_ADMIN_USER_ID": user_id,
            "JF_BOOTSTRAP_ADMIN_USERNAME": admin_username,
        },
    )
    return api_key, user_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--admin-username", default=DEFAULT_ADMIN_USERNAME)
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD)
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--library-name", required=True)
    parser.add_argument("--media-path", required=True)
    parser.add_argument("--collection-type", default="mixed")
    args = parser.parse_args()

    api_key, user_id = bootstrap_harness(
        base_url=args.base_url,
        config_path=Path(args.config),
        env_file=Path(args.env_file),
        username=args.admin_username,
        password=args.admin_password,
        server_name=args.server_name,
        app_name=args.app_name,
        timeout_seconds=args.timeout_seconds,
        library_name=args.library_name,
        media_path=args.media_path,
        collection_type=args.collection_type,
    )
    print(f"bootstrapped Jellyfin harness user_id={user_id} api_key={api_key}")


if __name__ == "__main__":
    main()
