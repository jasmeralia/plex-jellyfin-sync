# Spec Review — plex-jellyfin-sync

**Reviewer:** spec author (post-implementation audit)
**Date:** 2026-04-19
**Implementation tool:** Codex
**Test status at review time:** `174 passed, 36 skipped`
**Scope:** static analysis of source + tests against `spec.md`; no live functional tests run

---

## Summary verdict

The runtime is substantially complete and correct. All twelve spec modules exist, the SQLite schema matches §5.3, the FastAPI surface matches §5.4, and the non-destructive merge rules for user data are implemented correctly. Since the prior review (2026-04-18), all P0 and P1 gaps have been resolved: `merge_planner` is now wired into the sync engine, `MetadataDiff` dead fields are gone, dev dependencies are correct, the compose file has a network block, and the Dockerfile supports configurable UID/GID. Unit-test coverage across pure-function modules has grown from 121 to 174 tests.

The primary remaining gap is the functional test matrix (§7.2.2): 31 of 34 spec cases have test stubs, but all 31 are skipped because they need the docker-compose harness running. 3 spec cases (#2 idempotency, #4 add writer, #5 remove writer) have no stubs at all. The Jellyfin client unit tests remain thin at 9 tests vs the spec's ~15 required cases.

### Follow-up status (2026-04-19, post-review remediation)

After this review, the implementation and tests were updated again. Current local test status is `183 passed, 39 skipped`.

Resolved since this review:

- The collection startup-ordering bug in `SyncEngine._sync_collections()` was fixed. Collections with unresolved member mappings are now skipped for that pass instead of being deleted or partially rewritten.
- The missing unit coverage called out in this review was added for:
  - manual full-sync coalescing while already queued
  - positive deletion pruning (item gone from Plex and Jellyfin)
  - merged-item primary-only metadata and UserData targeting
  - Jellyfin client request shapes for merge/unmerge and UserData writes
  - explicit verification that runtime code does not call `mark_unplayed()`
  - merge-planner all-paths-unresolved deferral and primary stability
  - Plex per-account user-data field isolation across owner/non-owner reads
- Functional matrix cases `#2` idempotency, `#4` add writer, and `#5` remove writer now have harness tests.

Still open after remediation:

- Functional harness tests remain opt-in and skipped unless run with `pytest --functional-harness`, so the acceptance criterion that all 34 functional cases pass in CI is still not satisfied automatically.
- `JellyfinClient.find_item_by_path()` still performs a full library scan per lookup. This remains the known v1 performance tradeoff noted below.

---

## 1. Architecture (§2)

**Correct:**
- Single-process FastAPI + asyncio design matching §2.2 ✓
- All 12 modules from §5.2 present: `config`, `state`, `plex_client`, `jellyfin_client`, `path_mapper`, `mapper`, `user_data_merger`, `merge_planner`, `diff`, `sync_engine`, `webhook_server`, `app` ✓
- `merge_planner.plan_merge()` is now called from `SyncEngine._sync_item` ✓ *(was dead in prior review)*
- Startup full-sync issued on lifespan start ✓
- `SyncEngine` accepts injectable `requeue_callback`, `sleep_func`, `monotonic_func` ✓
- `codex_loop.py` and `responses_loop.py` are excluded from the Docker image via `RUN rm -f` in the Dockerfile ✓ *(fixed since prior review)*

---

## 2. Field mapping (§3.1)

**Correct:**
- Studio → `Studios` single-entry list ✓
- Writers → `People[Type=Actor]` (writer-as-actor remap) ✓
- Directors → `People[Type=Director]` ✓
- `LockedFields` set to `["Cast", "Studios"]` after write ✓
- `Studios=[]` when studio is `None` ✓
- `(name, role)` dedup key correctly handles same-name writer+director as two entries ✓
- Collections extracted as tuple of name strings ✓

**Minor gap:** No unit test for the "person appears as both writer and director produces two People entries" case (one Actor, one Director with same name). The `map_people()` implementation is correct; the test is just absent.

---

## 3. User data sync (§3.5)

**Correct.** `user_data_merger.merge_user_data()` implements all six non-destructive rules correctly and is now well tested (15 cases):
- Watched promotion and preservation ✓
- Play count promotion and no-regression (parametrized across 3 cases) ✓
- Rating update; `None` Plex rating treated as "no opinion" ✓
- Last-played set to `max(plex, jellyfin)` (5 cases covering None/None, Plex-only, Jellyfin-only, Plex wins, equal) ✓
- `changed` flag correctly gates API writes ✓

Per-account token support in `PlexClient.get_user_data(token=)` is implemented and routes through a fresh `PlexServer` per call. `userdata_changes` is logged on successful writes (`sync.userdata_updated`).

---

## 4. Collection sync (§3.2)

**Correct:**
- Creates BoxSet if not present ✓
- Set-diff add/remove for membership ✓
- Deletes empty/removed collections ✓
- Handles renamed collections ✓
- Handles smart collections transparently via `collection.items()` ✓

**Corner case (unchanged from prior review):** On a first full sync, if a collection's member items haven't yet been individually synced and have no `item_map` row, `desired_member_ids` will be empty and the collection will be incorrectly deleted then re-created when those items are synced in the same pass. This is a startup-ordering issue; subsequent syncs are correct.

---

## 5. Deletion semantics (§3.3)

**Correct:** `_prune_deleted_item` only removes the `item_map` row when both the Plex item is gone *and* the corresponding Jellyfin item is also gone. Tests cover:
- Keeps mapping when item gone from Plex but still in Jellyfin ✓
- Does not prune when Plex lookup errors ✓

**Missing test:** The positive deletion path — item gone from Plex *and* Jellyfin, row is pruned — still has no dedicated test.

---

## 6. Merge / alternate versions (§3.6)

**Correct:**
- `merge_planner.plan_merge()` is now the single implementation and is wired into `SyncEngine._sync_item` ✓
- Multi-path Plex items trigger `MergeVersions` with primary first ✓
- Primary selection matches `item.media[0].parts[0].file` ✓
- Plex-authoritative remerge: unmerges existing group then remerges ✓
- Warning log emitted on remerge overwrite ✓
- Unmerge propagation when Plex item reverts to single file ✓
- Deferred resolution triggers `POST /Library/Refresh` and requeues ✓
- `max_requeue_count` drop-and-log on exhaustion ✓

`merge_planner` now has 7 tests covering the main cases required by §7.1.6:
- `noop` for already-correct grouping ✓
- `defer` for unresolved paths ✓
- `unmerge` when previously merged, now single file ✓
- `merge` for multi-file item without current group ✓
- `rebuild` for wrong primary ✓
- `rebuild` for extra current member ✓
- Paths already in group treated as resolved ✓

**Still missing from §7.1.6:**
- All Jellyfin items unresolved → `defer` (partial — only some paths unresolved is tested)
- Primary selection stability: same Plex ordering → same primary id across repeated calls
- Primary selection with path mapping applied

---

## 7. Change detection and debounce (§4)

### Webhook events (§4.1)

**Correct:**
- `library.new` → per-item sync only (`submit_item_sync`) ✓ *(prior review noted double-enqueue; that is now fixed)*
- `media.scrobble`, `media.rate` → user-data sync for mapped account ✓
- All other events → ignored (200, no queue) ✓
- Unmapped account → accepted 200, nothing enqueued ✓
- Shared secret validation ✓

### Debounce queue (§4.4)

Well covered — 10 tests now (up from ~4):
- Per-item window dispatch and reset ✓
- Manual trigger clears and preempts when idle ✓
- Startup full sync dispatches immediately ✓
- Handler exception doesn't kill worker ✓
- Webhook full sync coalesces ✓
- Webhook full sync preempts pending per-item/user-data ✓
- Manual trigger during active sync queues exactly one follow-up and clears pending work at start ✓
- Independent windows for `library.new` and `media.scrobble` on same ratingKey ✓
- User-data window resets on additional events for same (ratingKey, account) ✓
- Requeue: restarts window, drops at `max_requeue_count` ✓

**Missing (minor):** Multiple manual triggers while one is already queued → verified coalesce to exactly one. The current tests cover the busy-then-trigger path but not the queued-trigger-then-trigger path explicitly.

### Concurrency (§4.5)

`_busy` flag + sequential worker loop correctly enforces "at most one sync at a time." Priority order (manual > webhook full > per-item) is implemented correctly.

---

## 8. Configuration (§5.1)

**Correct and well tested:**
- All schema fields and defaults ✓
- Env var substitution `${VAR}` and `${VAR:-default}` ✓
- `extra="forbid"` on `AppConfig` ✓
- Blank webhook secret normalised to `None` ✓
- Missing required fields raise errors — now parametrized across all 6 required fields (`plex.base_url`, `plex.token`, `plex.library_name`, `jellyfin.base_url`, `jellyfin.api_key`, `jellyfin.library_name`) ✓
- Invalid integer type produces validation error ✓
- Optional `jellyfin.user_id` can be omitted ✓

---

## 9. State (§5.3)

**Correct and now well tested:**
- All six tables match spec DDL ✓
- WAL mode ✓
- Foreign key CASCADE deletes enabled ✓
- `get_primary_and_sources()` helper ✓
- Schema migration v1 → v2 ✓
- `collection_map` CRUD ✓
- `person_cache` cache miss then hit ✓
- `user_data_map` CRUD with multiple users per item ✓
- CASCADE delete (item_map → media_source_map + user_data_map) ✓
- Unique constraint violation surfaces as `sqlite3.IntegrityError` ✓
- WAL mode confirmed in a dedicated test ✓

**Missing:**
- `list_item_maps()` not directly tested (covered indirectly through sync engine tests)

---

## 10. Plex client (§5.5)

**Correct:**
- Full path enumeration for merged items ✓
- Primary path = `item.media[0].parts[0].file` ✓
- Handles items with no writers/directors/studio ✓
- Retry with exponential backoff ✓
- Per-account token routes through alternate `PlexServer` ✓

**Still missing from §7.1.8:**
- Per-account token authentication failure raises a typed error *without* affecting owner-account reads
- Library name mismatch causes `PlexClientError` (the implementation raises this but it's not tested)
- `isWatched`, `viewCount`, `userRating`, `lastViewedAt` read separately for owner vs non-owner accounts

---

## 11. Jellyfin client (§5.4 / §7.1.9)

**Correct — all required endpoints implemented**, `X-Emby-Token` set, retry on 5xx, typed exceptions, `mark_played()` / `update_user_data()` / `merge_versions()` / `unmerge_versions()` / `MediaSources` parsing all present.

**Test coverage is thin (9 tests vs ~15 required).**

**Still missing from §7.1.9:**
- Explicit assertion that `DELETE /Users/{id}/PlayedItems/{id}` (`mark_unplayed`) is never called anywhere — spec calls for "verified no code path exists". The method exists on the client but no call site exists in the engine; a grep-based test or absence-of-call test would satisfy this.
- `POST /Videos/MergeVersions` request shape: IDs in correct order, primary first
- `DELETE /Videos/{id}/AlternateSources` call shape
- `MediaSources` array on a merged item parsed into `media_sources` tuple
- `update_user_data()` POST body shape (PlayCount, LastPlayedDate, Rating keys)
- `mark_played()` dedicated test (currently only exercised through sync engine integration)

**Performance concern (unchanged):** `find_item_by_path()` fetches the entire library and does a linear scan for a matching path — O(n) per unresolved path lookup. For large libraries with many unresolved paths (e.g. initial sync), this is quadratic. This is a known tradeoff and acceptable for v1 given the spec doesn't define an indexed path endpoint, but worth documenting.

---

## 12. Sync engine (§7.1.12)

Strong coverage overall. Key tests:
- Full sync: refresh, wait for item count, log timeout ✓
- Item event: persist mapping, update metadata, sync collections, sync user data ✓
- Item event survives user-data failure ✓
- Requeue on unresolved path, log error on exhaustion ✓
- Person cache warm/cold path ✓
- Merge, unmerge, remerge, disabled-merging paths ✓
- Stale path claim release ✓
- Keeps mapping when item gone from Plex but present in Jellyfin ✓

**Still missing:**
- Positive deletion path: item gone from Plex *and* Jellyfin → `item_map` row removed
- Full sync iterates all mapped users for user-data sync (assert every mapping entry produces a user-data sync attempt)
- For a 3-file merged item: `POST /Items/{id}` called exactly once, to the primary's ID
- UserData write on a merged item targets primary only; non-primary source IDs never used for UserData calls

---

## 13. Webhook server (§7.1.11)

Well covered. Minor remaining gap:
- `/readyz` test does not separately test the case where state is healthy but Plex or Jellyfin is unreachable (only the all-healthy and state-DB-down paths are tested)

---

## 14. Functional tests (§7.2.2) — STUBS PRESENT, NOT RUNNABLE

31 of the 34 spec cases now have test stubs in `test_functional_matrix_harness.py` (1745 lines). All 31 are **skipped** without the docker-compose Jellyfin harness running. Three spec cases have no stub:

| Spec case | Status |
|---|---|
| #1 Full sync from empty Jellyfin | stub, skipped |
| **#2 Idempotency** | **no stub** |
| #3 Studio change | stub, skipped |
| **#4 Add writer** | **no stub** |
| **#5 Remove writer** | **no stub** |
| #6–34 (remainder) | stubs, all skipped |

Acceptance criterion §11 #2 requires all 34 tests to pass in CI. Current status: 0 passing (all skipped), 3 missing stubs.

The harness infrastructure is in place (`docker-compose.test.yml`, `tests/harness/media/` fixtures, `harness_bootstrap.py`). The blocker is that the tests require the harness to be up and provisioned before `pytest` runs.

---

## 15. Deployment artifacts (§6)

### Dockerfile (§6.2)

**Correct:**
- `FROM python:3.12-slim` ✓
- `tini` installed ✓
- `SYNC_UID` / `SYNC_GID` build args (default 568, matching TrueNAS default) ✓ *(fixed since prior review)*
- Non-root user `sync` with configurable UID/GID ✓
- `codex_loop.py` / `responses_loop.py` removed before install ✓
- All required packages in `pyproject.toml` ✓
- `responses` in `[project.optional-dependencies] dev` ✓ *(fixed since prior review)*

### docker-compose.example.yml (§6.1)

**Correct:**
- Single service, `unless-stopped`, port 8089, correct volumes, env vars for tokens ✓
- `networks:` block present ✓ *(fixed since prior review)*

---

## 16. Observability (§8)

**Correct:**
- structlog JSON format ✓
- All required log fields present ✓
- `userdata_changes` list logged on successful writes (`sync.userdata_updated`) ✓
- Warning log on remerge overwrite ✓
- Sync summary log at end of each run ✓
- `/admin/sync-log`, `/admin/stats` ✓
- `/healthz`, `/readyz` ✓

**Minor:** The `requeue_count` field appears in drop events (`queue.drop.item`, `queue.drop.userdata`) but not in the intermediate requeue success log (`sync.requeued` only logs `requeue_count` on the requeued event itself, which is correct). No gap.

---

## 17. Spec non-goals correctly respected

All out-of-scope items from §1.2 are absent:
- No bidirectional sync, no scheduled polling, no multi-library, no artwork, no resume positions, no playlists, no subtitle/audio track metadata, no chapters ✓

---

## Prioritised gap list

| Priority | Gap | Spec reference |
|---|---|---|
| **P0** | Functional tests #2, #4, #5 have no stubs | §7.2.2 |
| **P0** | All 31 functional test stubs are skipped — no CI runner for harness | §7.2.2, §11 criterion 2 |
| **P1** | Jellyfin client: unplay-never-called assertion; MergeVersions/AlternateSources shape tests; UserData POST body shape; mark_played dedicated test | §7.1.9 |
| **P2** | Sync engine: positive deletion path; per-user iteration; merged-write targeting | §7.1.12 |
| **P2** | Plex client: per-account token failure; library name mismatch | §7.1.8 |
| **P2** | merge_planner: all-paths-unresolved defer; primary stability; path-mapped primary | §7.1.6 |
| **P2** | Webhook server: /readyz with Plex or Jellyfin unreachable | §7.1.11 |
| **P3** | `find_item_by_path` does full library scan per call — O(n) per unresolved path | §5.4 |
| **P3** | Collection startup-ordering: members with no item_map row cause transient empty-collection delete | §3.2 |
| **P3** | Mapper: same-name writer+director produces two People entries — untested | §7.1.4 |
