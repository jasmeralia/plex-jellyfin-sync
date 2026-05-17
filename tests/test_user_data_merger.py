from __future__ import annotations

from datetime import UTC, datetime

import pytest

from plex_jellyfin_sync.models import JellyfinUserData, PlexUserData
from plex_jellyfin_sync.user_data_merger import merge_user_data


def test_merge_user_data_never_regresses_watched_state() -> None:
    plex = PlexUserData(watched=False, play_count=1)
    jellyfin = JellyfinUserData(played=True, play_count=1)

    plan = merge_user_data(plex, jellyfin)

    assert plan.mark_played is False
    assert plan.changed is False


def test_merge_user_data_applies_non_destructive_updates() -> None:
    old = datetime(2024, 1, 1, tzinfo=UTC)
    new = datetime(2024, 2, 1, tzinfo=UTC)
    plex = PlexUserData(watched=True, play_count=3, rating=8.5, last_viewed_at=new)
    jellyfin = JellyfinUserData(played=False, play_count=1, rating=7.0, last_played_date=old)

    plan = merge_user_data(plex, jellyfin)

    assert plan.changed is True
    assert plan.mark_played is True
    assert plan.play_count == 3
    assert plan.rating == 8.5
    assert plan.last_played_date == new


@pytest.mark.parametrize(
    ("plex_watched", "jellyfin_played", "mark_played", "changed"),
    [
        (True, True, False, False),
        (False, False, False, False),
        (True, False, True, True),
        (False, True, False, False),
    ],
)
def test_merge_user_data_handles_all_watched_state_combinations(
    plex_watched: bool,
    jellyfin_played: bool,
    mark_played: bool,
    changed: bool,
) -> None:
    plan = merge_user_data(
        PlexUserData(watched=plex_watched, play_count=0),
        JellyfinUserData(played=jellyfin_played, play_count=0),
    )

    assert plan.mark_played is mark_played
    assert plan.changed is changed


@pytest.mark.parametrize(
    ("plex_count", "jellyfin_count", "expected_count", "changed"),
    [
        (1, 3, None, False),
        (3, 3, None, False),
        (4, 3, 4, True),
    ],
)
def test_merge_user_data_only_promotes_play_count(
    plex_count: int,
    jellyfin_count: int,
    expected_count: int | None,
    changed: bool,
) -> None:
    plan = merge_user_data(
        PlexUserData(play_count=plex_count),
        JellyfinUserData(play_count=jellyfin_count),
    )

    assert plan.play_count == expected_count
    assert plan.changed is changed


def test_merge_user_data_does_not_clear_jellyfin_rating_when_plex_has_none() -> None:
    plan = merge_user_data(
        PlexUserData(rating=None),
        JellyfinUserData(rating=8.0),
    )

    assert plan.rating is None
    assert plan.changed is False


def test_merge_user_data_skips_equal_rating() -> None:
    plan = merge_user_data(
        PlexUserData(rating=8.0),
        JellyfinUserData(rating=8.0),
    )

    assert plan.rating is None
    assert plan.changed is False


@pytest.mark.parametrize(
    ("plex_last", "jellyfin_last", "expected"),
    [
        (None, None, None),
        (datetime(2024, 1, 1, tzinfo=UTC), None, datetime(2024, 1, 1, tzinfo=UTC)),
        (None, datetime(2024, 1, 1, tzinfo=UTC), None),
        (
            datetime(2024, 2, 1, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 2, 1, tzinfo=UTC),
        ),
        (
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 2, 1, tzinfo=UTC),
            None,
        ),
    ],
)
def test_merge_user_data_uses_latest_last_played_timestamp(
    plex_last: datetime | None,
    jellyfin_last: datetime | None,
    expected: datetime | None,
) -> None:
    plan = merge_user_data(
        PlexUserData(last_viewed_at=plex_last),
        JellyfinUserData(last_played_date=jellyfin_last),
    )

    assert plan.last_played_date == expected
