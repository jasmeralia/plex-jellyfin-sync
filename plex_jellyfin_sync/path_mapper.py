from __future__ import annotations

from plex_jellyfin_sync.config import PathMappingRule


class PathMapper:
    def __init__(self, rules: list[PathMappingRule] | None = None) -> None:
        self._rules = sorted(
            [
                PathMappingRule(
                    plex_prefix=self._normalize_prefix(rule.plex_prefix),
                    jellyfin_prefix=self._normalize_prefix(rule.jellyfin_prefix),
                )
                for rule in (rules or [])
            ],
            key=lambda rule: len(rule.plex_prefix),
            reverse=True,
        )

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        PathMapper._reject_windows_path(prefix)
        if prefix == "/":
            return prefix
        return prefix.rstrip("/")

    @staticmethod
    def _reject_windows_path(path: str) -> None:
        if "\\" in path or (len(path) >= 2 and path[1] == ":"):
            raise ValueError(f"Windows-style paths are not supported: {path}")

    @staticmethod
    def _matches_prefix(path: str, prefix: str) -> bool:
        return path == prefix or path.startswith(f"{prefix}/")

    def map_plex_to_jellyfin(self, path: str) -> str:
        self._reject_windows_path(path)
        for rule in self._rules:
            if self._matches_prefix(path, rule.plex_prefix):
                suffix = path[len(rule.plex_prefix) :]
                return f"{rule.jellyfin_prefix}{suffix}"
        return path

    def map_jellyfin_to_plex(self, path: str) -> str:
        self._reject_windows_path(path)
        for rule in self._rules:
            if self._matches_prefix(path, rule.jellyfin_prefix):
                suffix = path[len(rule.jellyfin_prefix) :]
                return f"{rule.plex_prefix}{suffix}"
        return path
