from __future__ import annotations

from uuid import uuid4

import pytest

from plex_jellyfin_sync.config import load_config
from plex_jellyfin_sync.jellyfin_client import JellyfinClient


def _build_client(config_path: str) -> JellyfinClient:
    config = load_config(config_path)
    return JellyfinClient(
        base_url=config.jellyfin.base_url,
        api_key=config.jellyfin.api_key,
        library_name=config.jellyfin.library_name,
        user_id=config.jellyfin.user_id,
        request_timeout_seconds=config.jellyfin.request_timeout_seconds,
        max_retries=config.jellyfin.max_retries,
        retry_backoff_seconds=config.jellyfin.retry_backoff_seconds,
    )


@pytest.mark.live_jellyfin
def test_live_jellyfin_library_is_reachable(require_live_jellyfin, live_config_path) -> None:
    client = _build_client(str(live_config_path))

    items = client.list_library_items()

    assert isinstance(items, list)
    assert all(item.item_id for item in items)


@pytest.mark.live_jellyfin
def test_live_jellyfin_collection_listing_is_readable(require_live_jellyfin, live_config_path) -> None:
    client = _build_client(str(live_config_path))

    collections = client.list_collections()

    assert isinstance(collections, list)
    assert all(collection.collection_id for collection in collections)


@pytest.mark.live_jellyfin
def test_live_jellyfin_create_and_delete_collection(
    require_live_jellyfin_writes,
    live_config_path,
) -> None:
    client = _build_client(str(live_config_path))
    items = client.list_library_items()
    if not items:
        pytest.skip("live Jellyfin test library has no items to attach to a temporary collection")

    temp_name = f"plex-jellyfin-sync-live-{uuid4().hex[:10]}"
    collection_id = client.create_collection(temp_name, (items[0].item_id,))
    try:
        collections = {collection.name: collection for collection in client.list_collections()}
        assert temp_name in collections
        assert collections[temp_name].collection_id == collection_id
    finally:
        client.delete_item(collection_id)
