from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time

import pytest
import yaml

from plex_jellyfin_sync.harness_bootstrap import bootstrap_harness
from plex_jellyfin_sync.jellyfin_client import JellyfinClient, JellyfinClientError


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-plex",
        action="store_true",
        default=False,
        help="Run opt-in read-only tests against a real Plex server.",
    )
    parser.addoption(
        "--live-jellyfin",
        action="store_true",
        default=False,
        help="Run opt-in tests against a real Jellyfin server.",
    )
    parser.addoption(
        "--live-jellyfin-writes",
        action="store_true",
        default=False,
        help="Allow live Jellyfin tests that mutate server state. Use only with a dedicated test library.",
    )
    parser.addoption(
        "--live-config",
        action="store",
        default="config/config.yaml",
        help="Config file path used by opt-in live Plex tests.",
    )
    parser.addoption(
        "--functional-harness",
        action="store_true",
        default=False,
        help="Run opt-in functional matrix tests against the disposable local Jellyfin docker harness.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live_plex: opt-in test that hits a real Plex server")
    config.addinivalue_line("markers", "live_jellyfin: opt-in test that hits a real Jellyfin server")
    config.addinivalue_line("markers", "functional_harness: opt-in test that uses the disposable local Jellyfin docker harness")


@pytest.fixture
def live_config_path(request: pytest.FixtureRequest) -> Path:
    return Path(str(request.config.getoption("--live-config"))).resolve()


@pytest.fixture
def require_live_plex(request: pytest.FixtureRequest, live_config_path: Path) -> None:
    if not request.config.getoption("--live-plex"):
        pytest.skip("pass --live-plex to run live Plex validation")
    if not live_config_path.exists():
        pytest.skip(f"live config not found: {live_config_path}")


@pytest.fixture
def require_live_jellyfin(request: pytest.FixtureRequest, live_config_path: Path) -> None:
    if not request.config.getoption("--live-jellyfin"):
        pytest.skip("pass --live-jellyfin to run live Jellyfin validation")
    if not live_config_path.exists():
        pytest.skip(f"live config not found: {live_config_path}")


@pytest.fixture
def require_live_jellyfin_writes(
    request: pytest.FixtureRequest,
    require_live_jellyfin: None,
) -> None:
    if not request.config.getoption("--live-jellyfin-writes"):
        pytest.skip("pass --live-jellyfin-writes to allow live Jellyfin write validation")


@pytest.fixture(scope="session")
def require_functional_harness(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--functional-harness"):
        pytest.skip("pass --functional-harness to run the disposable Jellyfin harness tests")
    if shutil.which("docker") is None:
        pytest.skip("docker is required for the disposable Jellyfin harness tests")


def _compose_base_command(compose_file: Path) -> list[str]:
    if subprocess.run(["docker", "compose", "version"], check=False, capture_output=True, text=True).returncode == 0:
        return ["docker", "compose", "-f", str(compose_file)]
    if shutil.which("docker-compose"):
        return ["docker-compose", "-f", str(compose_file)]
    raise RuntimeError("docker compose is required for the disposable Jellyfin harness tests")


def _run_compose(compose_file: Path, *, cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_compose_base_command(compose_file), *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _wait_for_library_items(client: JellyfinClient, expected_count: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            client.trigger_library_refresh()
            if len(client.list_library_items()) >= expected_count:
                return
        except JellyfinClientError:
            pass
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for Jellyfin harness library to expose {expected_count} items")


@pytest.fixture(scope="session")
def functional_harness(require_functional_harness: None, tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parent.parent
    workspace = tmp_path_factory.mktemp("functional-harness")
    compose_file = workspace / "docker-compose.functional.yml"
    config_dir = workspace / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    env_path = config_dir / "jellyfin-bootstrap.env"
    example_config_path = repo_root / "tests" / "harness" / "config" / "config.test.example.yaml"

    source_media_dir = repo_root / "tests" / "harness" / "media"
    media_dir = workspace / "media"
    jellyfin_config_dir = workspace / "jellyfin-config"
    jellyfin_cache_dir = workspace / "jellyfin-cache"
    jellyfin_config_dir.mkdir(parents=True, exist_ok=True)
    jellyfin_cache_dir.mkdir(parents=True, exist_ok=True)

    if not (source_media_dir / "fixture-01.mp4").exists():
        generator = subprocess.run(
            ["bash", str(repo_root / "scripts" / "generate_test_media.sh")],
            check=False,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        if generator.returncode != 0:
            pytest.skip(f"unable to generate harness media fixtures: {generator.stderr.strip()}")

    shutil.copytree(source_media_dir, media_dir)

    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "jellyfin": {
                        "image": "jellyfin/jellyfin:10.10.7",
                        "ports": ["18096:8096"],
                        "volumes": [
                            f"{jellyfin_config_dir}:/config",
                            f"{jellyfin_cache_dir}:/cache",
                            f"{media_dir}:/media/othervideo:ro",
                        ],
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config_path.write_text(example_config_path.read_text(encoding="utf-8"), encoding="utf-8")
    if env_path.exists():
        env_path.unlink()

    _run_compose(compose_file, cwd=workspace, args=["down", "-v", "--remove-orphans"])
    up_result = _run_compose(compose_file, cwd=workspace, args=["up", "-d", "jellyfin"])
    if up_result.returncode != 0:
        pytest.skip(f"unable to start Jellyfin harness: {up_result.stderr.strip()}")

    try:
        api_key, user_id = bootstrap_harness(
            base_url="http://localhost:18096",
            config_path=config_path,
            env_file=env_path,
            username="admin",
            password="plex-jellyfin-sync",
            server_name="plex-jellyfin-sync-harness",
            app_name="plex-jellyfin-sync",
            timeout_seconds=180.0,
            library_name="Other Video",
            media_path="/media/othervideo",
            collection_type="mixed",
        )
        client = JellyfinClient(
            base_url="http://localhost:18096",
            api_key=api_key,
            library_name="Other Video",
        )
        _wait_for_library_items(client, 3, timeout_seconds=180.0)
        yield {
            "base_url": "http://localhost:18096",
            "api_key": api_key,
            "user_id": user_id,
            "client": client,
            "repo_root": repo_root,
            "config_path": config_path,
            "workspace": workspace,
            "compose_file": compose_file,
            "media_dir": media_dir,
        }
    finally:
        _run_compose(compose_file, cwd=workspace, args=["down", "-v", "--remove-orphans"])
