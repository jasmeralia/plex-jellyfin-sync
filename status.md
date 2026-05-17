# Status

Updated: 2026-04-18 20:30:04 PDT

## Current Position

The project is functionally close to `spec.md` on core service behavior, but it is not fully spec-complete.

Implemented and verified:

- Config loading, env substitution, and runtime toggles
- FastAPI webhook/manual/admin endpoints
- Debounce queue with manual/startup full-sync priority
- Startup full sync
- SQLite-backed item, media-source, collection, user-data, and sync-log state
- WAL-enabled SQLite state with version-aware initialization and legacy schema migration coverage
- Plex and Jellyfin client adapters with retry/backoff
- Item metadata sync from Plex to Jellyfin
- Collection materialization and reconciliation
- Non-destructive per-user watched/play-count/rating sync
- Merge, unmerge, and remerge behavior for alternate versions
- Refresh wait and requeue handling for unresolved Jellyfin items
- Explicit Jellyfin Person lookup with state-backed person cache hydration
- Structured logging and basic operator runbook/docs
- Docker/deployment artifacts and a disposable Jellyfin harness
- Opt-in `--live-plex` pytest scaffold for read-only real Plex validation
- Opt-in `--live-jellyfin` and `--live-jellyfin-writes` pytest scaffolds for dedicated Jellyfin test-library validation
- Initial fixture corpus under `tests/fixtures/plex` and `tests/fixtures/jellyfin`, now used by client tests

Current verification:

- `.venv/bin/python -m pytest -q` -> `174 passed, 36 skipped`
- Disposable Jellyfin matrix coverage is now implemented behind `--functional-harness`
- Real Jellyfin harness verification:
  - `.venv/bin/python -m pytest tests/test_functional_matrix_harness.py --functional-harness -q` -> `31 passed`

## Functional Matrix Tracker

Spec target: `spec.md` §7.2.2 tests 1–34

### Chunk 0. Harness Foundation

Status: `done`

Included work:

- Added a disposable `--functional-harness` pytest path
- Added a temp-mounted Docker compose fixture so each harness run gets a clean Jellyfin workspace
- Extended harness bootstrap to provision the Jellyfin test library automatically
- Verified the startup/bootstrap flow against a real Jellyfin `10.10.7` container and adjusted it to use the live startup-user sequence

Files:

- [tests/conftest.py](/home/morgan/git/plex-jellyfin-sync/tests/conftest.py)
- [plex_jellyfin_sync/harness_bootstrap.py](/home/morgan/git/plex-jellyfin-sync/plex_jellyfin_sync/harness_bootstrap.py)
- [tests/test_harness_bootstrap.py](/home/morgan/git/plex-jellyfin-sync/tests/test_harness_bootstrap.py)
- [scripts/smoke_test.sh](/home/morgan/git/plex-jellyfin-sync/scripts/smoke_test.sh)

### Chunk 1. Real Jellyfin Metadata Basics

Status: `done`

Spec cases in scope:

- `1.` Full-sync from empty Jellyfin
- `3.` Studio change
- `10.` Field lock

Implemented:

- Added the first real-Jellyfin functional tests in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py)
- These tests start a throwaway Jellyfin container, bootstrap it, create the test library, and drive the real `SyncEngine` against mocked Plex data
- Fixed the real Jellyfin `10.10.7` metadata write contract in [jellyfin_client.py](/home/morgan/git/plex-jellyfin-sync/plex_jellyfin_sync/jellyfin_client.py):
  - item writes now repost a fuller DTO with normalized `Genres`, `Tags`, and `ProviderIds`
  - `LockData` is set when `LockedFields` are managed
  - single-item reads now use `/Users/{userId}/Items/{itemId}` when `user_id` is configured so `LockedFields` are actually visible on readback

Verification:

- `.venv/bin/python -m pytest tests/test_functional_matrix_harness.py --functional-harness -q` -> chunk 1 cases are green inside the real disposable Jellyfin harness

### Chunk 2. Real Jellyfin Collections

Status: `done`

Spec cases in scope:

- `6.` Collection creation
- `7.` Collection membership change
- `8.` Collection deletion
- `9.` Smart collection materialisation

Implemented:

- Added collection-focused real-Jellyfin functional coverage in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py)
- Added harness polling helpers for BoxSet membership/deletion assertions so the tests measure final Jellyfin state rather than transient read-after-write lag

Verification:

- `.venv/bin/python -m pytest tests/test_functional_matrix_harness.py --functional-harness -q` -> chunk 1 and chunk 2 cases are green inside the real disposable Jellyfin harness

### Chunk 3. Real Jellyfin User-Data Semantics

Status: `done`

Spec cases completed in this chunk:

- `11.` Watched state promotion
- `12.` Watched state preservation
- `13.` Play count promotion
- `14.` Play count preservation
- `15.` Rating update
- `16.` Rating cleared in Plex
- `17.` Last-played monotonicity

Implemented:

- Added real-Jellyfin functional user-data coverage in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py)
- Extended the harness-side Plex mock to provide per-item user-data payloads through the real `SyncEngine` flow
- Added explicit Jellyfin user-state reset helpers in the harness tests so watched/play-count/rating assertions do not inherit stale session state across cases
- Added `mark_unplayed()` support to [jellyfin_client.py](/home/morgan/git/plex-jellyfin-sync/plex_jellyfin_sync/jellyfin_client.py) and unit coverage in [tests/test_jellyfin_client.py](/home/morgan/git/plex-jellyfin-sync/tests/test_jellyfin_client.py)
- Added webhook-driven user-data harness coverage for mapped and unmapped Plex accounts through the real `FastAPI` webhook path plus the real `DebounceQueue`

Verification:

- `.venv/bin/python -m pytest tests/test_functional_matrix_harness.py --functional-harness -q` -> chunk 3 cases are green

### Chunk 4. Manual Trigger And Debounce Semantics

Status: `done`

Spec cases in scope:

- `18.` Manual trigger endpoint (idle)
- `19.` Manual trigger clears debounce queue
- `20.` Manual trigger during active sync
- `21.` Webhook endpoint debounce
- `22.` Debounce reset

Implemented:

- Added real app/queue harness coverage for manual-trigger and webhook debounce behavior in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py)
- Fixed a real spec mismatch in [webhook_server.py](/home/morgan/git/plex-jellyfin-sync/plex_jellyfin_sync/webhook_server.py): `library.new` now enqueues only the per-item sync path, matching `spec.md`
- Fixed a real queue bug in [debounce_queue.py](/home/morgan/git/plex-jellyfin-sync/plex_jellyfin_sync/debounce_queue.py) where resetting a timer for the same key could drop the replacement task from `_timers`, causing `wait_for_idle()` to report idle too early under repeated events
- Updated webhook unit tests in [tests/test_webhook_server.py](/home/morgan/git/plex-jellyfin-sync/tests/test_webhook_server.py) to match the corrected `library.new` behavior

Verification:

- `.venv/bin/python -m pytest tests/test_functional_matrix_harness.py --functional-harness -q` -> `21 passed`
- `.venv/bin/python -m pytest tests/test_debounce_queue.py -q` -> `11 passed`
- `.venv/bin/python -m pytest tests/test_webhook_server.py -q` -> `17 passed`

### Chunk 5. Path Mapping And Restart Recovery

Status: `done`

Spec cases completed in this chunk:

- `25.` Restart recovery
- `26.` Path mapping

Implemented:

- Added real harness coverage for non-identity path mapping in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py)
- Extended the functional harness helpers to build `AppConfig` path-mapping rules and feed them through the real `SyncEngine` / `PathMapper` path instead of using pre-mapped Plex fixture paths
- Added restart-recovery coverage in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py) that interrupts a real Jellyfin container mid-full-sync, restarts it, and verifies the same SQLite state DB completes a fresh full sync cleanly afterward

Verification:

- `.venv/bin/python -m pytest tests/test_functional_matrix_harness.py --functional-harness -q` -> chunk 5 cases are green

### Planned Next Chunks

Status: `done`

### Chunk 6. Merge / Unmerge / Requeue Matrix

Status: `done`

Spec cases completed in this chunk:

- `27.` Multi-file merged item — initial merge
- `28.` Multi-file merged item — idempotency
- `29.` Multi-file merged item — metadata update
- `30.` Multi-file merged item — watched state preservation
- `31.` Plex unmerge propagation

Additional spec cases completed in this chunk:

- `32.` Jellyfin grouping disagrees with Plex
- `33.` Deferred merge via requeue
- `34.` Requeue exhaustion

Implemented:

- Added real harness coverage for initial multi-file merge, merge idempotency, merged metadata updates, merged watched-state preservation, and Plex-driven unmerge propagation in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py)
- Added merged-item harness helpers that assert the real Jellyfin `MediaSources` set on the primary item after merge/unmerge operations
- Added disagreement/rebuild coverage by pre-merging a partial Jellyfin alternate-version group and verifying the next sync unmerges and rebuilds it to match Plex
- Switched the disposable harness to use a per-run writable media copy in [tests/conftest.py](/home/morgan/git/plex-jellyfin-sync/tests/conftest.py), which makes deferred-appearance requeue cases possible without mutating checked-in fixtures
- Updated the functional webhook stack to use production-style requeue wiring and refresh tracking, then added end-to-end deferred-merge and requeue-exhaustion coverage in [tests/test_functional_matrix_harness.py](/home/morgan/git/plex-jellyfin-sync/tests/test_functional_matrix_harness.py)

Verification:

- `.venv/bin/python -m pytest tests/test_functional_matrix_harness.py --functional-harness -q` -> `31 passed`

## Not Fully Done Vs `spec.md`

### 1. Live-service execution is still outstanding

Files:

- [README.md](/home/morgan/git/plex-jellyfin-sync/README.md)
- [tests/harness/config/config.test.example.yaml](/home/morgan/git/plex-jellyfin-sync/tests/harness/config/config.test.example.yaml)

Gaps:

- The §7.2.2 disposable-harness matrix is now green end to end, but no confirmed run has been made against a real Plex server from this environment
- Live Jellyfin scaffolding exists, but no confirmed run has been made against a dedicated real Jellyfin test library from this environment

Impact:

- The code is verified against the full disposable functional matrix, but not yet against operator-provided live services from this environment

- This is the biggest remaining uncertainty before calling the project operationally complete

## Recommended Next Work Order

### Priority 1

Execute live validation:

- run the existing `--live-plex` read-only test path against a real server
- run the existing `--live-jellyfin` path against a dedicated Jellyfin test library
- run the write-capable `--live-jellyfin-writes` path only against that dedicated test library

### Priority 2

Broaden non-matrix polish:

- expand fixture variety beyond the minimal harness media corpus
- tighten docs around live-validation workflow and operator expectations
- decide whether any remaining spec items need additional live-service or smoke-test automation

## Bottom Line

If the question is "can this be tried now?", the answer is yes.

If the question is "is the repo fully done against `spec.md`?", the answer is no. The remaining work is mostly:

1. opt-in live Plex/Jellyfin validation
2. non-matrix polish and operational validation
