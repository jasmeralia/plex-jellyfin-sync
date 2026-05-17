from plex_jellyfin_sync.mapper import build_desired_metadata


def test_build_desired_metadata_maps_writers_to_actors_and_directors_directly() -> None:
    desired = build_desired_metadata(
        studio="Studio A",
        writers=["Alice", "Alice"],
        directors=["Bob"],
        collections=["Zeta", "Zeta", "Alpha"],
    )

    assert desired.studios == ("Studio A",)
    assert [(person.name, person.role) for person in desired.people] == [
        ("Alice", "Actor"),
        ("Bob", "Director"),
    ]
    assert desired.locked_fields == ("Cast", "Studios")
    assert desired.collections == ("Zeta", "Alpha")


def test_build_desired_metadata_keeps_distinct_roles_for_same_person_name() -> None:
    desired = build_desired_metadata(
        studio=None,
        writers=["Alice"],
        directors=["Alice"],
        collections=[],
    )

    assert [(person.name, person.role) for person in desired.people] == [
        ("Alice", "Actor"),
        ("Alice", "Director"),
    ]
