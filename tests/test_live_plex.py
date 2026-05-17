from __future__ import annotations

import pytest

from plex_jellyfin_sync.config import load_config
from plex_jellyfin_sync.plex_client import PlexClient


@pytest.mark.live_plex
def test_live_plex_library_is_reachable(require_live_plex, live_config_path) -> None:
    config = load_config(live_config_path)
    client = PlexClient(
        base_url=config.plex.base_url,
        token=config.plex.token,
        library_name=config.plex.library_name,
        request_timeout_seconds=config.plex.request_timeout_seconds,
        max_retries=config.plex.max_retries,
        retry_backoff_seconds=config.plex.retry_backoff_seconds,
    )

    items = client.list_items()

    assert isinstance(items, list)
    assert len(items) >= 1
    assert all(item.rating_key > 0 for item in items)


@pytest.mark.live_plex
def test_live_plex_collections_are_readable(require_live_plex, live_config_path) -> None:
    config = load_config(live_config_path)
    client = PlexClient(
        base_url=config.plex.base_url,
        token=config.plex.token,
        library_name=config.plex.library_name,
        request_timeout_seconds=config.plex.request_timeout_seconds,
        max_retries=config.plex.max_retries,
        retry_backoff_seconds=config.plex.retry_backoff_seconds,
    )

    collections = client.list_collections()

    assert isinstance(collections, list)
    assert all(collection.key > 0 for collection in collections)
