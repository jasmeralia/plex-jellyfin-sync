from __future__ import annotations

from plex_jellyfin_sync.models import DesiredMetadata, PersonRef


def _ordered_unique(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def map_people(writers: list[str] | tuple[str, ...], directors: list[str] | tuple[str, ...]) -> tuple[PersonRef, ...]:
    people: list[PersonRef] = []
    seen: set[tuple[str, str]] = set()

    for writer in writers:
        key = (writer, "Actor")
        if writer and key not in seen:
            seen.add(key)
            people.append(PersonRef(name=writer, role="Actor"))

    for director in directors:
        key = (director, "Director")
        if director and key not in seen:
            seen.add(key)
            people.append(PersonRef(name=director, role="Director"))

    return tuple(people)


def build_desired_metadata(
    *,
    studio: str | None,
    writers: list[str] | tuple[str, ...],
    directors: list[str] | tuple[str, ...],
    collections: list[str] | tuple[str, ...],
    lock_synced_fields: bool = True,
) -> DesiredMetadata:
    locked_fields = ("Cast", "Studios") if lock_synced_fields else ()
    studios = (studio,) if studio else ()
    return DesiredMetadata(
        studios=studios,
        people=map_people(writers, directors),
        locked_fields=locked_fields,
        collections=_ordered_unique(list(collections)),
    )
