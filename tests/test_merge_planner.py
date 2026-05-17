from plex_jellyfin_sync.merge_planner import plan_merge


def test_plan_merge_returns_noop_for_matching_grouping() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv", "/b.mkv"),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": "id-b"},
        current_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": "id-b"},
        current_primary_id="id-a",
    )

    assert plan.action == "noop"
    assert plan.primary_id == "id-a"


def test_plan_merge_returns_defer_for_unresolved_paths() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv", "/b.mkv"),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": None},
        current_path_to_item_id={},
        current_primary_id=None,
    )

    assert plan.action == "defer"
    assert plan.unresolved_paths == ("/b.mkv",)


def test_plan_merge_returns_unmerge_when_previously_merged_now_single_path() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv",),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": "id-a"},
        current_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": "id-b"},
        current_primary_id="id-a",
        previously_merged=True,
    )

    assert plan.action == "unmerge"
    assert plan.primary_id == "id-a"


def test_plan_merge_returns_defer_when_all_paths_are_unresolved() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv", "/b.mkv"),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": None, "/b.mkv": None},
        current_path_to_item_id={},
        current_primary_id=None,
    )

    assert plan.action == "defer"
    assert plan.unresolved_paths == ("/a.mkv", "/b.mkv")


def test_plan_merge_returns_merge_for_multifile_item_without_current_group() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv", "/b.mkv"),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": "id-b"},
        current_path_to_item_id={"/a.mkv": "id-a"},
        current_primary_id="id-a",
    )

    assert plan.action == "merge"
    assert plan.primary_id == "id-a"
    assert plan.ordered_ids == ("id-a", "id-b")


def test_plan_merge_returns_rebuild_for_wrong_primary() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv", "/b.mkv"),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": "id-b"},
        current_path_to_item_id={"/a.mkv": "src-a", "/b.mkv": "src-b"},
        current_primary_id="id-b",
        current_primary_path="/b.mkv",
    )

    assert plan.action == "rebuild"
    assert plan.primary_id == "id-a"


def test_plan_merge_returns_rebuild_for_extra_current_member() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv", "/b.mkv"),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": "id-b"},
        current_path_to_item_id={"/a.mkv": "src-a", "/b.mkv": "src-b", "/c.mkv": "src-c"},
        current_primary_id="id-a",
    )

    assert plan.action == "rebuild"
    assert plan.primary_id == "id-a"


def test_plan_merge_treats_paths_already_in_group_as_resolved() -> None:
    plan = plan_merge(
        desired_paths=("/a.mkv", "/b.mkv"),
        primary_path="/a.mkv",
        desired_path_to_item_id={"/a.mkv": "id-a", "/b.mkv": None},
        current_path_to_item_id={"/a.mkv": "src-a", "/b.mkv": "src-b"},
        current_primary_id="id-a",
    )

    assert plan.action == "noop"
    assert plan.primary_id == "id-a"


def test_plan_merge_keeps_primary_selection_stable_across_repeated_calls() -> None:
    first = plan_merge(
        desired_paths=("/mapped/a.mkv", "/mapped/b.mkv"),
        primary_path="/mapped/a.mkv",
        desired_path_to_item_id={"/mapped/a.mkv": "id-a", "/mapped/b.mkv": "id-b"},
        current_path_to_item_id={},
        current_primary_id=None,
    )
    second = plan_merge(
        desired_paths=("/mapped/a.mkv", "/mapped/b.mkv"),
        primary_path="/mapped/a.mkv",
        desired_path_to_item_id={"/mapped/a.mkv": "id-a", "/mapped/b.mkv": "id-b"},
        current_path_to_item_id={},
        current_primary_id=None,
    )

    assert first.action == "merge"
    assert second.action == "merge"
    assert first.primary_id == "id-a"
    assert second.primary_id == "id-a"
    assert first.ordered_ids == ("id-a", "id-b")
    assert second.ordered_ids == ("id-a", "id-b")
