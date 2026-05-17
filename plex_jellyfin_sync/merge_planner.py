from __future__ import annotations

from collections.abc import Mapping

from plex_jellyfin_sync.models import MergePlan


def select_primary_id(path_to_item_id: Mapping[str, str | None], primary_path: str) -> str | None:
    return path_to_item_id.get(primary_path)


def plan_merge(
    *,
    desired_paths: tuple[str, ...],
    primary_path: str,
    desired_path_to_item_id: Mapping[str, str | None],
    current_path_to_item_id: Mapping[str, str | None],
    current_primary_id: str | None,
    current_primary_path: str | None = None,
    previously_merged: bool = False,
) -> MergePlan:
    unresolved_paths = tuple(
        path
        for path in desired_paths
        if not desired_path_to_item_id.get(path) and path not in current_path_to_item_id
    )
    if unresolved_paths:
        return MergePlan(action="defer", unresolved_paths=unresolved_paths)

    primary_id = select_primary_id(desired_path_to_item_id, primary_path)
    desired_ids = tuple(desired_path_to_item_id[path] for path in desired_paths if desired_path_to_item_id.get(path))
    current_paths = tuple(path for path, item_id in current_path_to_item_id.items() if item_id)

    if len(desired_paths) <= 1:
        if previously_merged or len(current_paths) > 1:
            return MergePlan(action="unmerge", primary_id=current_primary_id or primary_id)
        return MergePlan(action="noop", primary_id=primary_id, ordered_ids=desired_ids)

    if (
        set(current_paths) == set(desired_paths)
        and current_primary_id == primary_id
        and (current_primary_path is None or current_primary_path == primary_path)
    ):
        return MergePlan(action="noop", primary_id=primary_id, ordered_ids=desired_ids)

    if len(current_paths) <= 1 or current_primary_id is None:
        return MergePlan(action="merge", primary_id=primary_id, ordered_ids=desired_ids)

    return MergePlan(action="rebuild", primary_id=primary_id, ordered_ids=desired_ids)
