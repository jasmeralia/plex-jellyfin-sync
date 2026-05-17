# Test Harness

This directory holds the local integration harness for `plex-jellyfin-sync`.

Contents:

- `media/`: tiny sample video files for Jellyfin to scan
- `config/`: harness config mounted into the sync container
- `state/`: SQLite state for the sync service
- `jellyfin/config/`: Jellyfin server config for the disposable test instance
- `jellyfin/cache/`: Jellyfin cache for the disposable test instance

The harness is intentionally local-only. It is not used by unit tests; it is used by the smoke path in [`scripts/smoke_test.sh`](/home/morgan/git/plex-jellyfin-sync/scripts/smoke_test.sh).
