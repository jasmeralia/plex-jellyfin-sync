from plex_jellyfin_sync.config import PathMappingRule
from plex_jellyfin_sync.path_mapper import PathMapper


def test_path_mapper_returns_original_path_when_no_rules() -> None:
    mapper = PathMapper()
    assert mapper.map_plex_to_jellyfin("/media/othervideo/file.mkv") == "/media/othervideo/file.mkv"


def test_path_mapper_translates_single_rule() -> None:
    mapper = PathMapper([PathMappingRule(plex_prefix="/data", jellyfin_prefix="/media")])

    assert mapper.map_plex_to_jellyfin("/data/file.mkv") == "/media/file.mkv"


def test_path_mapper_uses_longest_matching_prefix() -> None:
    mapper = PathMapper(
        [
            PathMappingRule(plex_prefix="/data", jellyfin_prefix="/media"),
            PathMappingRule(plex_prefix="/data/other", jellyfin_prefix="/media/other"),
        ]
    )

    assert mapper.map_plex_to_jellyfin("/data/other/file.mkv") == "/media/other/file.mkv"


def test_path_mapper_returns_original_path_when_no_rule_matches() -> None:
    mapper = PathMapper([PathMappingRule(plex_prefix="/data", jellyfin_prefix="/media")])

    assert mapper.map_plex_to_jellyfin("/library/file.mkv") == "/library/file.mkv"


def test_path_mapper_supports_reverse_mapping() -> None:
    mapper = PathMapper([PathMappingRule(plex_prefix="/plex/media", jellyfin_prefix="/jf/media")])

    assert mapper.map_jellyfin_to_plex("/jf/media/file.mkv") == "/plex/media/file.mkv"


def test_path_mapper_normalizes_trailing_slashes() -> None:
    mapper = PathMapper([PathMappingRule(plex_prefix="/plex/media/", jellyfin_prefix="/jf/media/")])

    assert mapper.map_plex_to_jellyfin("/plex/media/file.mkv") == "/jf/media/file.mkv"
    assert mapper.map_jellyfin_to_plex("/jf/media/file.mkv") == "/plex/media/file.mkv"


def test_path_mapper_does_not_match_partial_path_segments() -> None:
    mapper = PathMapper([PathMappingRule(plex_prefix="/data", jellyfin_prefix="/media")])

    assert mapper.map_plex_to_jellyfin("/database/file.mkv") == "/database/file.mkv"


def test_path_mapper_rejects_windows_style_paths() -> None:
    mapper = PathMapper([PathMappingRule(plex_prefix="/data", jellyfin_prefix="/media")])

    for path in ("C:\\media\\file.mkv", "C:/media/file.mkv", "\\\\server\\share\\file.mkv"):
        try:
            mapper.map_plex_to_jellyfin(path)
        except ValueError as exc:
            assert "Windows-style paths are not supported" in str(exc)
        else:
            raise AssertionError(f"Expected ValueError for {path}")
