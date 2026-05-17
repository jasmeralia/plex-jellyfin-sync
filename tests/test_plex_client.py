from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plex_jellyfin_sync.plex_client import PlexClient, PlexClientError


class FakeTag:
    def __init__(self, tag: str) -> None:
        self.tag = tag


class FakePart:
    def __init__(self, file: str) -> None:
        self.file = file


class FakeMedia:
    def __init__(self, *parts: str) -> None:
        self.parts = [FakePart(part) for part in parts]


class FakeItem:
    def __init__(self) -> None:
        self.ratingKey = 42
        self.media = [FakeMedia("/media/a.mkv", "/media/b.mkv")]
        self.studio = "Studio A"
        self.writers = [FakeTag("Alice")]
        self.directors = [FakeTag("Bob")]
        self.collections = [FakeTag("Favorites")]
        self.isWatched = True
        self.viewCount = 3
        self.userRating = 8.5
        self.lastViewedAt = datetime(2024, 1, 2, tzinfo=UTC)


class FakeCollection:
    def __init__(self, item: FakeItem) -> None:
        self.ratingKey = 10
        self.title = "Favorites"
        self._item = item

    def items(self):
        return [self._item]


class FakeSection:
    def __init__(self, item: FakeItem) -> None:
        self._item = item

    def all(self):
        return [self._item]

    def collections(self):
        return [FakeCollection(self._item)]

    def get(self, rating_key: str):
        if rating_key == "42":
            return self._item
        raise KeyError(rating_key)


class FakeLibrary:
    def __init__(self, item: FakeItem) -> None:
        self._section = FakeSection(item)

    def section(self, name: str):
        assert name == "Other Video"
        return self._section


class FakeServer:
    def __init__(self, item: FakeItem) -> None:
        self.library = FakeLibrary(item)


class EmptyMetadataItem(FakeItem):
    def __init__(self) -> None:
        super().__init__()
        self.studio = None
        self.writers = []
        self.directors = []
        self.collections = []


class RecordingServerFactory:
    def __init__(self, owner_item: FakeItem, alt_item: FakeItem | Exception) -> None:
        self.owner_item = owner_item
        self.alt_item = alt_item
        self.tokens: list[str] = []

    def __call__(self, _base_url: str, token: str):
        self.tokens.append(token)
        if token == "owner-token":
            return FakeServer(self.owner_item)
        if isinstance(self.alt_item, Exception):
            raise self.alt_item
        return FakeServer(self.alt_item)


def _fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "plex"


class JsonBackedTag:
    def __init__(self, tag: str) -> None:
        self.tag = tag


class JsonBackedPart:
    def __init__(self, file: str) -> None:
        self.file = file


class JsonBackedMedia:
    def __init__(self, parts: list[str]) -> None:
        self.parts = [JsonBackedPart(part) for part in parts]


class JsonBackedItem:
    def __init__(self, payload: dict) -> None:
        self.ratingKey = payload["ratingKey"]
        self.media = [JsonBackedMedia(media["parts"]) for media in payload["media"]]
        self.studio = payload["studio"]
        self.writers = [JsonBackedTag(tag) for tag in payload["writers"]]
        self.directors = [JsonBackedTag(tag) for tag in payload["directors"]]
        self.collections = [JsonBackedTag(tag) for tag in payload["collections"]]
        self.isWatched = payload["isWatched"]
        self.viewCount = payload["viewCount"]
        self.userRating = payload["userRating"]
        self.lastViewedAt = datetime.fromisoformat(payload["lastViewedAt"])


class JsonBackedCollection:
    def __init__(self, payload: dict, item: JsonBackedItem) -> None:
        self.ratingKey = payload["ratingKey"]
        self.title = payload["title"]
        self._item = item

    def items(self):
        return [self._item]


class JsonBackedSection:
    def __init__(self, item: JsonBackedItem, collection: JsonBackedCollection) -> None:
        self._item = item
        self._collection = collection

    def all(self):
        return [self._item]

    def collections(self):
        return [self._collection]

    def get(self, rating_key: str):
        if rating_key == str(self._item.ratingKey):
            return self._item
        raise KeyError(rating_key)


class JsonBackedLibrary:
    def __init__(self, section: JsonBackedSection) -> None:
        self._section = section

    def section(self, name: str):
        assert name == "Other Video"
        return self._section


class JsonBackedServer:
    def __init__(self, item: JsonBackedItem, collection: JsonBackedCollection) -> None:
        self.library = JsonBackedLibrary(JsonBackedSection(item, collection))


def test_plex_client_normalizes_items_collections_and_user_data() -> None:
    item = FakeItem()

    client = PlexClient(
        base_url="http://plex:32400",
        token="token",
        library_name="Other Video",
        server_factory=lambda _base_url, _token: FakeServer(item),
    )

    normalized = client.get_item(42)
    collections = client.list_collections()
    user_data = client.get_user_data(42)

    assert normalized is not None
    assert normalized.paths == ("/media/a.mkv", "/media/b.mkv")
    assert normalized.primary_path == "/media/a.mkv"
    assert normalized.writers == ("Alice",)
    assert normalized.directors == ("Bob",)
    assert collections[0].name == "Favorites"
    assert collections[0].member_rating_keys == (42,)
    assert user_data is not None
    assert user_data.play_count == 3
    assert user_data.rating == 8.5


def test_plex_client_normalizes_fixture_backed_item_and_collection() -> None:
    item_payload = json.loads((_fixtures_dir() / "item_merged.json").read_text(encoding="utf-8"))
    collection_payload = json.loads((_fixtures_dir() / "collection_favorites.json").read_text(encoding="utf-8"))
    item = JsonBackedItem(item_payload)
    collection = JsonBackedCollection(collection_payload, item)

    client = PlexClient(
        base_url="http://plex:32400",
        token="token",
        library_name="Other Video",
        server_factory=lambda _base_url, _token: JsonBackedServer(item, collection),
    )

    normalized = client.get_item(42)
    collections = client.list_collections()

    assert normalized is not None
    assert normalized.paths == ("/media/a.mkv", "/media/b.mkv")
    assert normalized.primary_path == "/media/a.mkv"
    assert normalized.writers == ("Alice",)
    assert collections[0].name == "Favorites"
    assert collections[0].member_rating_keys == (42,)


def test_plex_client_retries_transient_failures() -> None:
    item = FakeItem()
    attempts = {"count": 0}
    sleeps = []

    def flaky_server_factory(_base_url: str, _token: str):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporary plex failure")
        return FakeServer(item)

    client = PlexClient(
        base_url="http://plex:32400",
        token="token",
        library_name="Other Video",
        server_factory=flaky_server_factory,
        max_retries=2,
        retry_backoff_seconds=0.25,
        sleep_func=lambda seconds: sleeps.append(seconds),
    )

    normalized = client.get_item(42)

    assert normalized is not None
    assert attempts["count"] == 3
    assert sleeps == [0.25, 0.5]


def test_plex_client_raises_after_retry_exhaustion() -> None:
    client = PlexClient(
        base_url="http://plex:32400",
        token="token",
        library_name="Other Video",
        server_factory=lambda _base_url, _token: (_ for _ in ()).throw(RuntimeError("plex down")),
        max_retries=1,
        sleep_func=lambda _seconds: None,
    )

    with pytest.raises(PlexClientError):
        client.list_items()


def test_plex_client_get_item_raises_when_lookup_fails_transiently() -> None:
    class FailingSection(FakeSection):
        def get(self, rating_key: str):
            raise RuntimeError(f"temporary failure for {rating_key}")

    class FailingLibrary(FakeLibrary):
        def __init__(self, item: FakeItem) -> None:
            self._section = FailingSection(item)

    class FailingServer(FakeServer):
        def __init__(self, item: FakeItem) -> None:
            self.library = FailingLibrary(item)

    client = PlexClient(
        base_url="http://plex:32400",
        token="token",
        library_name="Other Video",
        server_factory=lambda _base_url, _token: FailingServer(FakeItem()),
        max_retries=1,
        sleep_func=lambda _seconds: None,
    )

    with pytest.raises(PlexClientError):
        client.get_item(42)


def test_plex_client_normalizes_missing_optional_metadata() -> None:
    client = PlexClient(
        base_url="http://plex:32400",
        token="token",
        library_name="Other Video",
        server_factory=lambda _base_url, _token: FakeServer(EmptyMetadataItem()),
    )

    normalized = client.get_item(42)

    assert normalized is not None
    assert normalized.studio is None
    assert normalized.writers == ()
    assert normalized.directors == ()
    assert normalized.collections == ()


def test_plex_client_get_user_data_uses_per_account_token_without_affecting_owner_reads() -> None:
    owner_item = FakeItem()
    alt_item = FakeItem()
    alt_item.isWatched = False
    alt_item.viewCount = 7
    alt_item.userRating = 9.0
    alt_item.lastViewedAt = datetime(2024, 1, 3, tzinfo=UTC)
    server_factory = RecordingServerFactory(owner_item, alt_item)
    client = PlexClient(
        base_url="http://plex:32400",
        token="owner-token",
        library_name="Other Video",
        server_factory=server_factory,
    )

    owner_user_data = client.get_user_data(42)
    alt_user_data = client.get_user_data(42, token="alt-token")
    owner_user_data_again = client.get_user_data(42)

    assert owner_user_data is not None
    assert alt_user_data is not None
    assert owner_user_data_again is not None
    assert owner_user_data.watched is True
    assert owner_user_data.play_count == 3
    assert owner_user_data.rating == 8.5
    assert owner_user_data.last_viewed_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert alt_user_data.watched is False
    assert alt_user_data.play_count == 7
    assert alt_user_data.rating == 9.0
    assert alt_user_data.last_viewed_at == datetime(2024, 1, 3, tzinfo=UTC)
    assert owner_user_data_again.watched is True
    assert owner_user_data_again.play_count == 3
    assert owner_user_data_again.rating == 8.5
    assert owner_user_data_again.last_viewed_at == datetime(2024, 1, 2, tzinfo=UTC)
    assert server_factory.tokens == ["owner-token", "alt-token", "owner-token"]


def test_plex_client_raises_typed_error_for_per_account_token_failure_without_breaking_owner_reads() -> None:
    owner_item = FakeItem()
    server_factory = RecordingServerFactory(owner_item, RuntimeError("alt token unauthorized"))
    client = PlexClient(
        base_url="http://plex:32400",
        token="owner-token",
        library_name="Other Video",
        server_factory=server_factory,
        max_retries=0,
    )

    owner_user_data = client.get_user_data(42)

    with pytest.raises(PlexClientError):
        client.get_user_data(42, token="alt-token")

    owner_user_data_again = client.get_user_data(42)

    assert owner_user_data is not None
    assert owner_user_data_again is not None
    assert owner_user_data.play_count == 3
    assert owner_user_data_again.play_count == 3


def test_plex_client_raises_when_library_lookup_fails() -> None:
    class WrongLibrary:
        def section(self, name: str):
            raise KeyError(name)

    class WrongLibraryServer:
        def __init__(self) -> None:
            self.library = WrongLibrary()

    client = PlexClient(
        base_url="http://plex:32400",
        token="token",
        library_name="Other Video",
        server_factory=lambda _base_url, _token: WrongLibraryServer(),
        max_retries=0,
    )

    with pytest.raises(PlexClientError):
        client.list_items()
