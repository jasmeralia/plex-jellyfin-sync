from __future__ import annotations

import hashlib
import json

from plex_jellyfin_sync.models import DesiredMetadata, MetadataDiff, PersonRef


def _sorted_people(people: tuple[PersonRef, ...]) -> list[dict[str, str]]:
    return [
        {"name": person.name, "role": person.role}
        for person in sorted(people, key=lambda item: (item.role, item.name))
    ]


def compute_content_hash(metadata: DesiredMetadata) -> str:
    payload = {
        "studios": sorted(metadata.studios),
        "people": _sorted_people(metadata.people),
        "locked_fields": sorted(metadata.locked_fields),
        "collections": sorted(metadata.collections),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def diff_metadata(current: DesiredMetadata, desired: DesiredMetadata) -> MetadataDiff:
    current_people = set(current.people)
    desired_people = set(desired.people)

    people_to_add = tuple(sorted(desired_people - current_people))
    people_to_remove = tuple(sorted(current_people - desired_people))

    item_changed = any(
        [
            set(current.studios) != set(desired.studios),
            set(current.locked_fields) != set(desired.locked_fields),
            current_people != desired_people,
        ]
    )

    return MetadataDiff(
        update_item=item_changed,
        people_to_add=people_to_add,
        people_to_remove=people_to_remove,
    )
