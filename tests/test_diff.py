from plex_jellyfin_sync.diff import compute_content_hash, diff_metadata
from plex_jellyfin_sync.models import DesiredMetadata, PersonRef


def test_compute_content_hash_is_order_insensitive_for_set_like_fields() -> None:
    left = DesiredMetadata(
        studios=("Studio A",),
        people=(PersonRef("Alice", "Actor"), PersonRef("Bob", "Director")),
        locked_fields=("Cast", "Studios"),
        collections=("One", "Two"),
    )
    right = DesiredMetadata(
        studios=("Studio A",),
        people=(PersonRef("Bob", "Director"), PersonRef("Alice", "Actor")),
        locked_fields=("Studios", "Cast"),
        collections=("Two", "One"),
    )

    assert compute_content_hash(left) == compute_content_hash(right)


def test_diff_metadata_detects_people_changes_without_using_collection_membership() -> None:
    current = DesiredMetadata(
        studios=("Studio A",),
        people=(PersonRef("Alice", "Actor"),),
        collections=("One",),
    )
    desired = DesiredMetadata(
        studios=("Studio A",),
        people=(PersonRef("Zoe", "Actor"),),
        collections=("Two",),
    )

    diff = diff_metadata(current, desired)

    assert diff.update_item is True
    assert diff.people_to_add == (PersonRef("Zoe", "Actor"),)
    assert diff.people_to_remove == (PersonRef("Alice", "Actor"),)
