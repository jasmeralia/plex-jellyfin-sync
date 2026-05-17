from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import requests

from plex_jellyfin_sync.jellyfin_client import JellyfinClient, JellyfinNotFoundError
from plex_jellyfin_sync.models import DesiredMetadata


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b"" if payload is None else b"{}"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError("http error")
            error.response = self
            raise error

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json, timeout))
        return self.responses.pop(0)


def _fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "jellyfin"


def test_jellyfin_client_normalizes_library_items_and_user_data() -> None:
    session = FakeSession(
        [
            FakeResponse([{"Name": "Other Video", "ItemId": "lib-1"}]),
            FakeResponse(
                {
                    "Items": [
                        {
                            "Id": "item-1",
                            "Path": "/media/a.mkv",
                            "Studios": [{"Name": "Studio A"}],
                            "People": [{"Name": "Alice", "Type": "Actor"}],
                            "LockedFields": ["Cast", "Studios"],
                            "MediaSources": [{"Id": "src-1", "Path": "/media/a.mkv"}],
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "UserData": {
                        "Played": True,
                        "PlayCount": 4,
                        "Rating": 8.0,
                        "LastPlayedDate": datetime(2024, 1, 1, tzinfo=UTC).isoformat(),
                    }
                }
            ),
            FakeResponse(None),
            FakeResponse(None),
        ]
    )
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    items = client.list_library_items()
    user_data = client.get_user_data("user-1", "item-1")
    client.mark_played("user-1", "item-1")
    client.update_user_data(
        "user-1",
        "item-1",
        play_count=5,
        rating=9.0,
        last_played_date=datetime(2024, 1, 2, tzinfo=UTC),
    )

    assert items[0].item_id == "item-1"
    assert items[0].metadata.studios == ("Studio A",)
    assert user_data.play_count == 4
    assert session.calls[1][2]["ParentId"] == "lib-1"
    assert session.calls[3][0] == "POST"
    assert session.calls[4][3]["PlayCount"] == 5


def test_jellyfin_client_normalizes_fixture_backed_item_and_person_search() -> None:
    item_payload = json.loads((_fixtures_dir() / "item_with_metadata.json").read_text(encoding="utf-8"))
    people_payload = json.loads((_fixtures_dir() / "person_search_alice.json").read_text(encoding="utf-8"))
    session = FakeSession(
        [
            FakeResponse([{"Name": "Other Video", "ItemId": "lib-1"}]),
            FakeResponse({"Items": [item_payload]}),
            FakeResponse(people_payload),
        ]
    )
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    items = client.list_library_items()
    person_id = client.find_person_id_by_name("Alice")

    assert items[0].metadata.people[0].name == "Alice"
    assert items[0].media_sources == (("/media/a.mkv", "src-1"),)
    assert person_id == "person-1"


def test_jellyfin_client_handles_collections_and_not_found() -> None:
    session = FakeSession(
        [
            FakeResponse({"Items": [{"Id": "box-1", "Name": "Favorites"}]}),
            FakeResponse({"Items": [{"Id": "item-1"}, {"Id": "item-2"}]}),
            FakeResponse({"Id": "box-2"}),
            FakeResponse({"Items": [{"Id": "box-1", "Name": "Favorites"}]}),
            FakeResponse(None),
            FakeResponse(None),
            FakeResponse(None),
            FakeResponse({"Items": [{"Id": "person-1", "Name": "Alice"}]}),
            FakeResponse(None, status_code=404),
        ]
    )
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    collections = client.list_collections()
    created_id = client.create_collection("New Collection", ("item-1",))
    client.rename_collection("box-1", "Renamed")
    client.add_items_to_collection("box-1", ("item-3",))
    client.remove_items_from_collection("box-1", ("item-2",))
    person_id = client.find_person_id_by_name("Alice")
    deleted = client.get_item_or_none("missing")

    assert collections[0].collection_id == "box-1"
    assert collections[0].item_ids == ("item-1", "item-2")
    assert created_id == "box-2"
    assert session.calls[2][0] == "POST"
    assert session.calls[3] == (
        "GET",
        "http://jellyfin:8096/Items",
        {
            "Ids": "box-1",
            "Fields": "Path,People,Studios,LockedFields,MediaSources,Genres,Tags,ProviderIds,ProductionLocations,Taglines",
        },
        None,
        15.0,
    )
    assert session.calls[4] == (
        "POST",
        "http://jellyfin:8096/Items/box-1",
        None,
        {
            "Id": "box-1",
            "Name": "Renamed",
            "Genres": [],
            "Tags": [],
            "ProviderIds": {},
            "ProductionLocations": [],
            "Taglines": [],
            "People": [],
            "Studios": [],
            "LockedFields": [],
        },
        15.0,
    )
    assert person_id == "person-1"
    assert deleted is None


def test_jellyfin_client_find_item_by_path_uses_configured_library() -> None:
    session = FakeSession(
        [
            FakeResponse([{"Name": "Other Video", "ItemId": "lib-1"}]),
            FakeResponse({"Items": [{"Id": "item-1", "Path": "/media/a.mkv"}]}),
        ]
    )
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    item = client.find_item_by_path("/media/a.mkv")

    assert item is not None
    assert session.calls[1][2]["ParentId"] == "lib-1"


def test_jellyfin_client_get_item_uses_user_scoped_endpoint_when_user_id_is_configured() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "Id": "item-1",
                    "Path": "/media/a.mkv",
                    "Studios": [{"Name": "Studio A"}],
                    "People": [{"Name": "Alice", "Type": "Actor"}],
                    "LockedFields": ["Cast", "Studios"],
                    "MediaSources": [{"Id": "src-1", "Path": "/media/a.mkv"}],
                }
            )
        ]
    )
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        user_id="user-1",
        session=session,
    )

    item = client.get_item("item-1")

    assert item.metadata.locked_fields == ("Cast", "Studios")
    assert session.calls[0] == (
        "GET",
        "http://jellyfin:8096/Users/user-1/Items/item-1",
        {"Fields": "Path,People,Studios,LockedFields,MediaSources"},
        None,
        15.0,
    )


def test_jellyfin_client_merge_and_unmerge_use_expected_endpoints() -> None:
    session = FakeSession([FakeResponse(None), FakeResponse(None)])
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    client.merge_versions(("item-1", "item-2", "item-3"))
    client.unmerge_versions("item-1")

    assert session.calls[0] == (
        "POST",
        "http://jellyfin:8096/Videos/MergeVersions",
        {"ids": "item-1,item-2,item-3"},
        None,
        15.0,
    )
    assert session.calls[1] == (
        "DELETE",
        "http://jellyfin:8096/Videos/item-1/AlternateSources",
        None,
        None,
        15.0,
    )


def test_jellyfin_client_mark_played_and_update_user_data_use_expected_requests() -> None:
    session = FakeSession([FakeResponse(None), FakeResponse(None), FakeResponse(None)])
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    client.mark_played("user-1", "item-1")
    client.update_user_data(
        "user-1",
        "item-1",
        play_count=5,
        rating=9.0,
        last_played_date=datetime(2024, 1, 2, tzinfo=UTC),
    )

    assert session.calls[0] == (
        "POST",
        "http://jellyfin:8096/Users/user-1/PlayedItems/item-1",
        None,
        None,
        15.0,
    )
    assert session.calls[1] == (
        "POST",
        "http://jellyfin:8096/Users/user-1/Items/item-1/UserData",
        None,
        {
            "PlayCount": 5,
            "Rating": 9.0,
            "LastPlayedDate": datetime(2024, 1, 2, tzinfo=UTC).isoformat(),
        },
        15.0,
    )


def test_jellyfin_client_mark_unplayed_uses_expected_request() -> None:
    session = FakeSession([FakeResponse(None)])
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    client.mark_unplayed("user-1", "item-1")

    assert session.calls[0] == (
        "DELETE",
        "http://jellyfin:8096/Users/user-1/PlayedItems/item-1",
        None,
        None,
        15.0,
    )


def test_runtime_never_calls_mark_unplayed() -> None:
    package_dir = Path(__file__).resolve().parent.parent / "plex_jellyfin_sync"
    call_sites: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        if path.name == "jellyfin_client.py":
            continue
        if ".mark_unplayed(" in path.read_text(encoding="utf-8"):
            call_sites.append(path.name)

    assert call_sites == []


def test_jellyfin_client_update_item_metadata_preserves_item_name_and_normalizes_nullable_fields() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "Items": [
                        {
                            "Id": "item-1",
                            "Name": "Fixture 01",
                            "Path": "/media/a.mkv",
                            "ProviderIds": None,
                        }
                    ]
                }
            ),
            FakeResponse(None),
        ]
    )
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
    )

    client.update_item_metadata(
        "item-1",
        DesiredMetadata(studios=("Studio A",), people=(), locked_fields=("Cast", "Studios")),
    )

    assert session.calls[0] == (
        "GET",
        "http://jellyfin:8096/Items",
        {
            "Ids": "item-1",
            "Fields": "Path,People,Studios,LockedFields,MediaSources,Genres,Tags,ProviderIds,ProductionLocations,Taglines",
        },
        None,
        15.0,
    )
    assert session.calls[1] == (
        "POST",
        "http://jellyfin:8096/Items/item-1",
        None,
        {
            "Id": "item-1",
            "Name": "Fixture 01",
            "Path": "/media/a.mkv",
            "Genres": [],
            "Tags": [],
            "ProviderIds": {},
            "ProductionLocations": [],
            "Taglines": [],
            "Studios": [{"Name": "Studio A"}],
            "People": [],
            "LockedFields": ["Cast", "Studios"],
            "LockData": True,
        },
        15.0,
    )


def test_jellyfin_client_retries_transient_5xx_failures() -> None:
    sleeps = []
    session = FakeSession(
        [
            FakeResponse(None, status_code=500),
            FakeResponse(None, status_code=502),
            FakeResponse([{"Name": "Other Video", "ItemId": "lib-1"}]),
            FakeResponse({"Items": []}),
        ]
    )
    client = JellyfinClient(
        base_url="http://jellyfin:8096",
        api_key="key",
        library_name="Other Video",
        session=session,
        max_retries=2,
        retry_backoff_seconds=0.25,
        sleep_func=lambda seconds: sleeps.append(seconds),
    )

    items = client.list_library_items()

    assert items == []
    assert len(session.calls) == 4
    assert sleeps == [0.25, 0.5]
