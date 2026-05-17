from __future__ import annotations

from datetime import datetime

from plex_jellyfin_sync.models import JellyfinUserData, PlexUserData, UserDataMergePlan


def _latest(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def merge_user_data(plex: PlexUserData, jellyfin: JellyfinUserData) -> UserDataMergePlan:
    mark_played = plex.watched and not jellyfin.played
    play_count = plex.play_count if plex.play_count > jellyfin.play_count else None

    rating = None
    if plex.rating is not None and plex.rating != jellyfin.rating:
        rating = plex.rating

    merged_last_played = _latest(plex.last_viewed_at, jellyfin.last_played_date)
    last_played_changed = merged_last_played != jellyfin.last_played_date
    last_played_date = merged_last_played if last_played_changed else None

    changed = any(
        [
            mark_played,
            play_count is not None,
            rating is not None,
            last_played_changed,
        ]
    )

    return UserDataMergePlan(
        changed=changed,
        mark_played=mark_played,
        play_count=play_count,
        rating=rating,
        last_played_date=last_played_date,
    )
