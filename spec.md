# plex-jellyfin-sync — Design Specification

**Version:** 1.0 (v1 scope)
**Target host:** TrueNAS SCALE (Goldeye), x86_64
**Deployment:** Docker Compose stack
**Author role:** Senior architect specification — no implementation code

---

## 1. Purpose and Scope

`plex-jellyfin-sync` is a Python service that replicates a curated subset of metadata from a Plex Media Server into a Jellyfin server. Jellyfin acts as a **read-only mirror** of Plex for the fields and collections defined in this spec. The sync is **one-way**, from Plex to Jellyfin. Any metadata edits made directly in Jellyfin will be overwritten on the next sync cycle affecting that item.

### 1.1 In scope (v1)

- Single Plex library (type: "Other Video") mirrored to a single Jellyfin library
- Field-level metadata replication for a fixed set of fields (see §3.1)
- Collection membership replication, including Plex smart collections materialised as static Jellyfin collections
- Non-destructive watched state replication (never regress Jellyfin watched → unwatched)
- Non-destructive play count replication (never regress Jellyfin play count)
- User rating replication
- Plex merged-item replication as Jellyfin alternate versions (see §3.6)
- Webhook-driven change detection with manual full-sync trigger
- Item deletion reconciliation for removed Plex items: stale sync state is pruned once the item is gone from both Plex and Jellyfin; Jellyfin still owns its own library/file deletion lifecycle
- Persistent item-ID mapping and metadata state for diff-based syncs
- Deployment as a Docker Compose stack on TrueNAS SCALE

### 1.2 Out of scope (v1)

- Bidirectional sync of editable metadata fields
- Scheduled polling fallback (explicitly deferred in favour of webhooks + manual trigger)
- Multi-library support
- Artwork/poster replication
- Per-user playback position (resume points)
- Playlists (distinct from collections)
- Subtitle track and audio track metadata
- Chapters

### 1.3 Non-goals

- Supporting Plex libraries other than the single "Other Video" library
- Being a general-purpose Plex-to-Jellyfin migration tool for third-party use
- Supporting Jellyfin instances not running on the same host with the same media mount

---

## 2. Architecture Overview

### 2.1 Components

The stack consists of a single Docker service (`sync`) plus its persistent state volume. Plex and Jellyfin are assumed to be running as separate services on the same TrueNAS host (deployed via the TrueNAS app catalog) and are **not** managed by this compose file.

```
┌─────────────────────── TrueNAS Goldeye ───────────────────────┐
│                                                               │
│  ┌──────────┐     ┌──────────┐     ┌───────────────────────┐  │
│  │   Plex   │     │ Jellyfin │     │ plex-jellyfin-sync    │  │
│  │ (app     │     │ (app     │     │                       │  │
│  │ catalog) │     │ catalog) │     │  ┌─────────────────┐  │  │
│  │          │     │          │     │  │ Webhook Server  │  │  │
│  │  ────┐   │     │          │     │  │  (FastAPI, 8089)│  │  │
│  │      │   │     │          │     │  └────────┬────────┘  │  │
│  │  webhook │     │          │     │           │           │  │
│  │      │   │     │          │     │  ┌────────▼────────┐  │  │
│  │      └───┼─────┼──────────┼─────┼─▶│ Debounce Queue  │  │  │
│  │          │     │          │     │  └────────┬────────┘  │  │
│  │   API    │     │   API    │     │           │           │  │
│  │    ▲     │     │    ▲     │     │  ┌────────▼────────┐  │  │
│  │    └─────┼─────┼────┼─────┼─────┼──│  Sync Engine    │  │  │
│  │          │     │    └─────┼─────┼─▶│                 │  │  │
│  └──────────┘     └──────────┘     │  └────────┬────────┘  │  │
│                                    │           │           │  │
│                                    │  ┌────────▼────────┐  │  │
│                                    │  │  State Store    │  │  │
│                                    │  │  (SQLite)       │  │  │
│                                    │  └─────────────────┘  │  │
│                                    └───────────────────────┘  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  /mnt/tank/media/othervideo  (ZFS dataset, shared)     │  │
│  │  Mounted identically into all three containers          │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

### 2.2 Runtime processes within the `sync` container

The container runs a single Python process that starts:

- **Webhook HTTP server** — FastAPI app listening on container port 8089
- **Debounce queue worker** — in-process asyncio task that coalesces events within a debounce window and dispatches to the sync engine
- **Sync engine** — performs the Plex-read / Jellyfin-write pipeline
- **Manual trigger endpoint** — HTTP endpoint that enqueues a full-library sync

A single-process design is chosen for simplicity and because the workload is IO-bound and low-throughput.

### 2.3 Data flow

1. User edits metadata in Plex, adds/removes items from a collection, or adds/removes a file
2. Plex fires webhook → `sync` container's webhook server
3. Webhook is classified (new item, metadata update, collection update, delete) and placed on the debounce queue
4. After the debounce window expires with no further events for the same item, the sync engine processes the event
5. Sync engine reads current state from Plex via `plexapi`, reads current state from SQLite, diffs, writes to Jellyfin
6. SQLite state is updated to reflect successful writes

### 2.4 Identity and joining

- **Plex identity:** `ratingKey` (stable integer per Plex item)
- **Jellyfin identity:** item `Id` (GUID string per Jellyfin item)
- **Join key:** container-absolute file path. Because Plex and Jellyfin mount the same host dataset at the same in-container path (see §6.3), the path from `plexapi.Media.parts[].file` matches the path from Jellyfin's `Path` field on the item.
- **Mapping persistence:** SQLite table `item_map(plex_rating_key, jellyfin_id, path, last_synced_at, content_hash)`

---

## 3. Field and Collection Mapping

### 3.1 Field mapping (v1)

| Plex source | Jellyfin destination | Transform |
|---|---|---|
| `item.studio` | `Studios` (list, single entry) | Direct copy, create Studio entity in Jellyfin if missing |
| `item.writers` (list of `Writer` objects) | `People` entries with `Type="Actor"` | **Semantic remap**: each Plex Writer name becomes a Jellyfin Actor (upsert Person entity) |
| `item.directors` (list of `Director` objects) | `People` entries with `Type="Director"` | Direct copy, upsert Person entity |
| Collection membership (`item.collections`) | Collection membership in Jellyfin | Set-diff add/remove (see §3.2) |
| `item.isWatched` / `item.viewCount` | `UserData.Played` per Jellyfin user | **Non-destructive merge** (see §3.5) |
| `item.viewCount` | `UserData.PlayCount` per Jellyfin user | **Non-destructive merge** (see §3.5) |
| `item.userRating` | `UserData.Rating` per Jellyfin user | Direct copy (see §3.5) |

**Fields explicitly not synced in v1:** title, summary, genres, tags, content rating, artwork, year, dates, resume position (`viewOffset`).

**Person entity behaviour:**
- Jellyfin People are first-class entities with their own IDs
- When a person name does not yet exist in Jellyfin, the sync creates it (upsert)
- Name is the canonical match key; no attempt is made to disambiguate two different real people with the same name
- No image or provider IDs are written to the Person entity

### 3.2 Collection mapping

All Plex collections attached to in-scope items are materialised as Jellyfin **BoxSet** collections (Jellyfin's collection type). Smart and manual collections in Plex are treated identically on the Jellyfin side — both become static Jellyfin collections whose membership is maintained by the sync engine.

- **Source of truth for membership:** result of `plex_collection.items()` at sync time (via `plexapi`), which transparently handles both smart and manual Plex collections
- **Jellyfin collection naming:** identical to Plex collection name (no prefix in v1, per user confirmation that no pre-existing manual Jellyfin collections exist)
- **Creation:** if a Jellyfin collection with the target name does not exist, create it
- **Membership diff:** sync engine computes `plex_members - jellyfin_members` (to add) and `jellyfin_members - plex_members` (to remove) and applies both
- **Empty collections:** if a Plex collection has zero items, the corresponding Jellyfin collection is deleted
- **Plex collection deletion:** if a collection name tracked in SQLite no longer exists in Plex, the corresponding Jellyfin collection is deleted

### 3.3 Deletion semantics

- **Item removed from Plex library (file still present or not):** the corresponding Jellyfin item is not deleted by this service. Jellyfin will detect the file removal on its own library scan. The sync service will remove the `item_map` row once it confirms the item no longer exists in either system.
- **Item removed from a Plex collection:** the item is removed from the corresponding Jellyfin collection on next sync.
- **Plex collection deleted:** the Jellyfin collection is deleted on next sync. Underlying items are not affected.

### 3.4 Metadata lock behaviour

After writing metadata to a Jellyfin item, the sync service sets `LockedFields` on the item to include `Studios`, `Cast` (People), and does **not** lock the other fields Jellyfin infers from filename or NFO. This prevents Jellyfin's scheduled library refresh from overwriting sync-managed fields while allowing Jellyfin to continue managing everything else.

`UserData` fields (watched, play count, rating) are not affected by `LockedFields` — they are stored in per-user data, not item metadata.

### 3.5 User data sync semantics

Unlike item metadata (which is per-item), watched state, play count, and rating are stored per Jellyfin user. These fields require special handling because:

1. Plex's watched/play-count values should never cause Jellyfin to *lose* progress
2. Ratings are subjective per user, but v1 treats Plex as authoritative

#### 3.5.1 User mapping

A new config section maps Plex accounts to Jellyfin users:

```yaml
user_mapping:
  # Each entry maps one Plex account to one Jellyfin user.
  # The Plex account name is as shown in the Plex home/managed user list;
  # use the Plex owner account's username (or email) for the primary user.
  - plex_account: "jas"
    jellyfin_user_id: "<guid>"
```

Only items watched by a mapped Plex account sync user data to the corresponding Jellyfin user. Unmapped Plex accounts are ignored. If the mapping is empty, user data sync is disabled entirely (field mapping still runs).

#### 3.5.2 Fetching Plex user data

For each mapped Plex account, the sync service queries Plex's `/status/sessions/history` and item-level view data. `plexapi` exposes `item.viewCount`, `item.isWatched`, `item.lastViewedAt`, and `item.userRating` as the owner-account values by default. For non-owner accounts, the sync service uses `MyPlexAccount.switchHomeUser()` or the `X-Plex-Token` of that specific account to read per-account values. The implementation will use per-account tokens configured in `user_mapping`:

```yaml
user_mapping:
  - plex_account: "jas"
    plex_token: "<that-user's-token>"   # optional; defaults to top-level plex.token for owner
    jellyfin_user_id: "<guid>"
```

#### 3.5.3 Non-destructive merge rules

For each `(plex_item, jellyfin_item, user)` triple:

- **Watched state:**
  - If Plex is watched and Jellyfin is unwatched: mark Jellyfin as watched
  - If Plex is unwatched and Jellyfin is watched: **no change** (do not regress Jellyfin)
  - If both agree: no write
- **Play count:**
  - If `plex.viewCount > jellyfin.playCount`: set Jellyfin play count to Plex value
  - If `plex.viewCount ≤ jellyfin.playCount`: **no change**
- **Rating:**
  - If Plex has a rating and Jellyfin's rating differs from Plex's: write Plex's rating
  - If Plex has no rating (`None`) and Jellyfin has a rating: **no change** (v1 treats a missing Plex rating as "no opinion", not "cleared")
  - Rating scales: Plex `userRating` is 0–10 float; Jellyfin `UserData.Rating` is 0–10 float. Direct pass-through.
- **Last played timestamp:** Jellyfin's `UserData.LastPlayedDate` is set to `max(plex.lastViewedAt, jellyfin.LastPlayedDate)`; never regressed.

These rules are implemented in the `user_data_merger` module as pure functions for unit testability.

#### 3.5.4 Jellyfin user data endpoints

| Purpose | Endpoint |
|---|---|
| Get item user data | `GET /Users/{userId}/Items/{itemId}?Fields=UserData` |
| Mark played | `POST /Users/{userId}/PlayedItems/{itemId}` |
| Mark unplayed | `DELETE /Users/{userId}/PlayedItems/{itemId}` (not used in v1 — watched never regresses) |
| Update user data (play count, rating, last-played) | `POST /Users/{userId}/Items/{itemId}/UserData` with body `{PlayCount, LastPlayedDate, Rating}` |

### 3.6 Merged items (Plex Merge → Jellyfin Alternate Versions)

Plex's "Merge" feature unifies multiple video files under a single metadata item. A merged Plex item has `len(item.media) > 1` (or a single `Media` with multiple `parts`), each referencing a distinct file on disk. Jellyfin has no UI-level equivalent but does support the same underlying data model via **alternate versions** (also called "alternate sources"), which can be created programmatically via the `MergeVersions` endpoint.

The sync service replicates Plex merge groupings as Jellyfin alternate-version groupings.

#### 3.6.1 Identity model

A Plex merged item has:

- One `ratingKey`
- One metadata payload (Studios, People, Collections, UserData)
- N file paths, accessed via `[part.file for m in item.media for part in m.parts]`
- A notion of **primary file**: `item.media[0].parts[0].file` — Plex's own ordering of versions. This is what Plex's UI shows first and what is used as the default playback target.

A Jellyfin merged item has:

- One primary item `Id` (the group's identity)
- One metadata payload on the primary item
- One or more `MediaSources` on the primary, each with its own `Path` and internal identifiers
- The non-primary source items retain their own Jellyfin `Id` in some internal sense but do not appear as top-level items after merging

#### 3.6.2 Primary version selection

When the sync service creates a Jellyfin alternate-version group, the **primary** is selected to match Plex's primary: the Jellyfin item whose `Path` equals `item.media[0].parts[0].file` (after path mapping). All other paths are merged into this primary. This keeps "which version plays by default" consistent between Plex and Jellyfin.

#### 3.6.3 Merge operations

For a Plex item with N ≥ 2 file paths:

1. Look up the Jellyfin item for each path via the per-path state table (§5.3)
2. If any path has no corresponding Jellyfin item (file not yet scanned by Jellyfin), apply the deferral rule in §3.6.6
3. Identify the primary Jellyfin item per §3.6.2
4. Compare the current Jellyfin grouping to the desired grouping:
   - **Not yet grouped:** call `POST /Videos/MergeVersions?ids=<primary>,<alt1>,<alt2>,...`
   - **Grouped correctly:** no action
   - **Grouped incorrectly** (different member set or different primary): call `DELETE /Videos/<currentPrimary>/AlternateSources` to unmerge, then `POST /Videos/MergeVersions` with the correct set
5. Write item metadata (Studios, People, Collections, LockedFields) to the primary Jellyfin item only
6. Update the state tables

For a Plex item with N = 1 file path (non-merged):

1. If the corresponding Jellyfin item is part of an alternate-version group (carry-over state from a previous sync), unmerge it: `DELETE /Videos/<primary>/AlternateSources`
2. Proceed with ordinary single-item sync

#### 3.6.4 Unmerge propagation

When a Plex item that was previously merged is unmerged by the user (split back into separate items in Plex), the next sync observes multiple Plex `ratingKey`s where there used to be one. Detection and action:

1. On sync, for each Plex ratingKey known to the state, check its current file-path set
2. If a path that previously belonged to `ratingKey_old` is now reported by Plex as belonging to `ratingKey_new` (or missing from `ratingKey_old` entirely), mark `ratingKey_old` as potentially split
3. Call `DELETE /Videos/<primary>/AlternateSources` on the Jellyfin primary of the old group
4. Proceed to sync each new Plex ratingKey as a separate item

This path is exercised rarely (user-corrected merge mistakes) but must work correctly.

#### 3.6.5 Plex authoritative resolution

Per user requirement, Plex is authoritative for merge grouping. If the sync service finds an existing Jellyfin alternate-version group that doesn't match Plex's grouping — whether Jellyfin was manually merged outside the sync service, or the spec changed between versions, or a merge was done in Plex that conflicts — the sync service **unmerges the existing Jellyfin group and remerges to match Plex**. This is destructive to Jellyfin-side manual merge decisions and is documented as an intentional behaviour.

The only Jellyfin state this does not override is UserData (watched, playcount) per §3.5.3, which continues to be non-destructively merged.

#### 3.6.6 Deferred resolution when Jellyfin hasn't scanned files yet

If the sync service cannot find a Jellyfin item for one or more paths in a Plex merged item, this indicates Jellyfin's scanner hasn't yet ingested those files. Handling differs by sync scope:

- **Full sync (manual trigger or startup):** the sync service calls `POST /Library/Refresh` at the start of the sync and polls `GET /Library/VirtualFolders` or a representative item count until a configurable timeout (default **120 seconds**) passes or the expected items appear. Any items still unresolved after the timeout are logged and skipped; they will be retried on the next sync.
- **Debounced per-item sync (webhook):** the sync service calls `POST /Library/Refresh` for the library and **requeues the event into the debounce queue with a fresh window**. Because the per-item debounce window is 15 minutes, Jellyfin's scan will complete well before the event re-dispatches. The event carries a `requeue_count` field; if it has been requeued 3 times without resolution, it is logged as a hard error and dropped until the next full sync.

The requeue mechanism intentionally reuses the existing debounce infrastructure rather than inventing a separate poll-wait loop.

#### 3.6.7 User data on merged items

Jellyfin tracks UserData per item (not per media source), matching Plex's behaviour on merged items. No special handling is needed for user-data sync on merged items — the merge rules in §3.5.3 apply to the primary Jellyfin item.

#### 3.6.8 State implications

The current single-row-per-path-and-per-ratingKey model doesn't accommodate merged items, where one ratingKey maps to multiple paths and (pre-merge) multiple Jellyfin items that collapse to one primary post-merge. See §5.3 for the updated schema.

---

## 4. Change Detection

### 4.1 Webhook events

Plex webhooks (Plex Pass feature) are configured to POST to `http://<sync-container>:8089/webhook/plex`. Relevant event types:

| Plex event | Sync action |
|---|---|
| `library.new` | Enqueue per-item sync after debounce |
| `media.scrobble` | Enqueue per-item user-data sync after short debounce (60s) — captures watched state when Plex marks an item watched |
| `media.rate` | Enqueue per-item user-data sync after short debounce (60s) |
| `library.on.deck` | Ignored |
| `media.play`, `media.pause`, `media.resume`, `media.stop` | Ignored |

**Webhook account attribution:** Plex webhooks include an `Account` field identifying which user's action triggered the event. The sync service uses this to select the matching Jellyfin user from `user_mapping`. Events from unmapped Plex accounts are dropped.

**Known limitation:** Plex does not emit a webhook for metadata edits (Studio, Writer, Director changes) or for collection membership changes. These are caught via the manual trigger (§4.3) or implicitly on the next `library.new` event for any item in the library (which causes a full library scan — see §4.2).

**Separate debounce windows per event class:**
- New item (`library.new`): 15-minute per-item debounce (long — user may still be editing metadata)
- User data (`media.scrobble`, `media.rate`): 60-second per-item debounce (short — no editing happening, just capturing finalised playback state)

### 4.2 Scan strategy

The sync engine supports three modes, selected per event:

- **Single-item metadata sync:** given one `ratingKey`, read that item from Plex, resolve its file list (may be multi-file/merged — §3.6), compute diff, apply any required merge/unmerge operations on the Jellyfin side, write metadata to the primary Jellyfin item. Includes a user-data pass for all mapped users at the end.
- **Single-item user-data sync:** given one `ratingKey` and one Plex account, read only that item's per-user state (watched, viewCount, rating, lastViewedAt) for that account, merge per §3.5.3, write to the mapped Jellyfin user's UserData on the primary Jellyfin item. Does not touch item metadata or merge grouping.
- **Full library sync:** iterate all items in the configured Plex library, plus all collections attached to those items, plus user data for all mapped accounts. Used for manual trigger, initial bootstrap, and for `library.new` when the item is new.

**Jellyfin library refresh behaviour:**

- **Full sync:** issues `POST /Library/Refresh` at the start of the run and polls for completion (or a fixed timeout of 120 seconds, configurable) before proceeding. Unresolved items after the timeout are logged and skipped; they will be picked up on the next sync.
- **Per-item debounced sync:** if the item's file list cannot be fully resolved on the Jellyfin side (one or more paths have no corresponding Jellyfin item), the sync service issues `POST /Library/Refresh` for the library and re-enqueues the event into the debounce queue with a fresh window. The event's `requeue_count` is incremented; after 3 unsuccessful requeues the event is logged as a hard error and dropped until the next full sync. This reuses the debounce infrastructure as a natural wait mechanism (15 minutes gives Jellyfin ample time to scan).
- **User-data sync:** operates on already-known items only. If the `ratingKey` is not yet in `item_map`, the event is requeued identically to per-item metadata events.

### 4.3 Manual trigger

A POST to `http://<sync-container>:8089/trigger/full-sync` triggers a full library sync. This is the intended mechanism to pick up metadata edits that don't generate webhook events. Intended usage: user edits metadata in Plex, then hits the manual trigger (via curl, a browser bookmark, or a Home Assistant button).

**Behaviour:**
- If no sync is currently running: all pending debounced events in the queue are cleared, and the full sync starts immediately
- If a sync is currently running: the full sync is enqueued to run immediately after the current one completes; pending debounced events are cleared at that point (not at trigger time, so events arriving during the in-flight sync are still cleared)
- Multiple manual triggers arriving while a sync is already queued coalesce into a single pending full sync

Response: HTTP 202 Accepted with a job ID; sync runs asynchronously. A second endpoint `GET /trigger/status/{job_id}` returns job state (queued, running, complete, failed) from a short-retention in-memory job table.

### 4.4 Debounce

Events entering the queue for a specific `ratingKey` are held for a configurable debounce window (default: **15 minutes / 900 seconds**) before dispatch. If another event for the same `ratingKey` arrives during the window, the window resets. This long window gives the user time to add and iterate on initial metadata in Plex after copying a new file into the library without triggering multiple sync passes.

A manual full-sync trigger (§4.3) **clears the entire debounce queue immediately** — both per-item debounced events and any pending full-sync debounce are discarded, and the manual full sync takes over. This is the intended behaviour: if the user explicitly asked for a full sync, they have finished editing and want results now.

**Full sync debounce:** full-sync events arriving via webhook-driven library-wide changes use a shorter debounce window (default: **30 seconds**). Multiple full-sync events coalesce to one dispatch. A manual trigger takes precedence and clears these as well.

### 4.5 Concurrency

At most one sync operation runs at a time. The queueing priority is:

1. **Manual full-sync trigger** — runs immediately if idle; otherwise runs as the next operation after the current sync completes. Clears all other pending work at the moment it becomes the active sync.
2. **Webhook-driven full sync** — runs after the short debounce window expires, unless superseded by a manual trigger.
3. **Per-item debounced sync** — runs after its 15-minute debounce window expires, unless superseded by a full sync (manual or webhook-driven) which makes the per-item work redundant.

If a manual trigger arrives while a manual full sync is already queued, they coalesce to a single pending manual full sync.

If a per-item sync is in progress and a manual trigger arrives, the per-item sync completes first (it's already running), then the manual full sync runs. The full sync will naturally include the item that was just synced, which is idempotent (no extra writes).

---

## 5. Component Specification

### 5.1 Configuration

All configuration is provided through a mounted `config.yaml` file. No environment variables from `.env` files; tokens are embedded directly in the compose file as container environment variables or as a mounted config file (see §6.1).

```yaml
# /config/config.yaml schema
plex:
  base_url: "http://plex:32400"
  token: "<plex-x-plex-token>"
  library_name: "Other Video"

jellyfin:
  base_url: "http://jellyfin:8096"
  api_key: "<jellyfin-api-key>"
  user_id: "<jellyfin-admin-user-id>"
  library_name: "Other Video"

user_mapping:
  # Maps Plex accounts to Jellyfin users for watched/playcount/rating sync.
  # Leave empty to disable user data sync entirely.
  - plex_account: "jas"
    plex_token: null            # optional; null means use top-level plex.token
    jellyfin_user_id: "<guid>"

path_mapping:
  # Map Plex-container paths to Jellyfin-container paths.
  # With same-path mounts on both (recommended), this is a pass-through.
  rules: []

sync:
  debounce_seconds: 900            # 15 minutes for per-item webhook events
  full_sync_debounce_seconds: 30   # short debounce for coalescing webhook full syncs
  field_mapping:
    studio: true
    writers_as_actors: true
    directors: true
    collections: true
  user_data:
    watched: true
    play_count: true
    rating: true
  merging:
    enabled: true
    refresh_timeout_seconds: 120   # how long to wait for Jellyfin scan during full sync
    max_requeue_count: 3           # max debounce requeues for unresolvable items
  lock_synced_fields: true

webhook:
  listen_port: 8089
  shared_secret: null

logging:
  level: INFO
  format: json

state:
  sqlite_path: "/state/sync.db"
```

### 5.2 Sync Engine modules

The sync engine is structured as these internal modules:

| Module | Responsibility |
|---|---|
| `config` | Load and validate `config.yaml`; expose typed config object |
| `state` | SQLite access layer; encapsulates schema, migrations, CRUD on `item_map`, `collection_map`, `user_data_map`, `sync_log` |
| `plex_client` | Thin wrapper over `plexapi`; fetches library, items, collections; normalises data; supports per-account token switching |
| `jellyfin_client` | Thin wrapper over `jellyfin-apiclient-python` plus direct `requests` calls for endpoints the library doesn't expose (People upsert, BoxSet CRUD, LockedFields, UserData) |
| `path_mapper` | Applies configured path prefix rewrites to translate Plex paths into Jellyfin paths |
| `mapper` | Pure functions converting Plex item → intended Jellyfin state; handles the Writer→Actor semantic remap |
| `user_data_merger` | Pure functions applying the non-destructive merge rules for watched / play count / rating / last-played |
| `merge_planner` | Pure functions computing the required Jellyfin MergeVersions / AlternateSources operations from the Plex item's file list and the current Jellyfin grouping state |
| `diff` | Pure functions computing what changes need to be applied to Jellyfin given current state + intended state |
| `sync_engine` | Orchestrator: fetches, diffs, applies, updates state, logs |
| `webhook_server` | FastAPI app; classifies events; enqueues |
| `debounce_queue` | In-memory asyncio-based queue with per-key debounce and priority-based preemption by manual triggers |
| `app` | Composition root: wires modules, starts server, handles signals |

### 5.3 State schema (SQLite)

```sql
CREATE TABLE item_map (
    -- One row per Plex ratingKey; maps to one Jellyfin primary item.
    -- For merged Plex items, jellyfin_primary_id is the post-merge primary.
    plex_rating_key       INTEGER PRIMARY KEY,
    jellyfin_primary_id   TEXT NOT NULL,
    is_merged             INTEGER NOT NULL DEFAULT 0,  -- 0 = single file, 1 = merged group
    content_hash          TEXT NOT NULL,  -- hash of synced fields for fast diff
    last_synced_at        TEXT NOT NULL   -- ISO8601
    -- NOTE: jellyfin_primary_id is NOT UNIQUE. A single Jellyfin primary id
    -- always corresponds to a single Plex ratingKey in v1, but we rely on
    -- application logic for this rather than a DB constraint to keep
    -- re-merge operations simpler.
);

CREATE TABLE media_source_map (
    -- One row per distinct file path. For merged Plex items, multiple rows
    -- share the same plex_rating_key and the same jellyfin_primary_id.
    path                  TEXT PRIMARY KEY,
    plex_rating_key       INTEGER NOT NULL,
    jellyfin_source_id    TEXT NOT NULL,
    -- pre-merge Jellyfin item id; post-merge this may equal jellyfin_primary_id
    -- for the primary source and differ for alternates
    is_primary            INTEGER NOT NULL DEFAULT 0,  -- matches Plex's item.media[0].parts[0].file
    FOREIGN KEY (plex_rating_key) REFERENCES item_map(plex_rating_key) ON DELETE CASCADE
);

CREATE INDEX idx_media_source_ratingkey ON media_source_map(plex_rating_key);
CREATE INDEX idx_media_source_jellyfin_id ON media_source_map(jellyfin_source_id);

CREATE TABLE collection_map (
    plex_collection_key INTEGER PRIMARY KEY,
    jellyfin_id         TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    last_synced_at      TEXT NOT NULL
);

CREATE TABLE person_cache (
    -- Optimisation: avoid round-tripping for every person name
    name           TEXT PRIMARY KEY,
    jellyfin_id    TEXT NOT NULL
);

CREATE TABLE user_data_map (
    -- Tracks last-synced per-user state to avoid redundant writes.
    -- Keyed on plex_rating_key (not per-source), matching the one-UserData-per-item model.
    plex_rating_key      INTEGER NOT NULL,
    jellyfin_user_id     TEXT NOT NULL,
    last_plex_viewcount  INTEGER NOT NULL DEFAULT 0,
    last_plex_watched    INTEGER NOT NULL DEFAULT 0,  -- boolean as 0/1
    last_plex_rating     REAL,
    last_plex_lastviewed TEXT,  -- ISO8601
    last_synced_at       TEXT NOT NULL,
    PRIMARY KEY (plex_rating_key, jellyfin_user_id),
    FOREIGN KEY (plex_rating_key) REFERENCES item_map(plex_rating_key) ON DELETE CASCADE
);

CREATE TABLE sync_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    trigger           TEXT NOT NULL,   -- 'webhook' | 'manual' | 'startup'
    scope             TEXT NOT NULL,   -- 'full' | 'item:<ratingKey>' | 'userdata:<ratingKey>:<userId>'
    items_examined    INTEGER,
    items_updated     INTEGER,
    user_data_updated INTEGER,
    merges_applied    INTEGER,
    unmerges_applied  INTEGER,
    requeued_events   INTEGER,
    errors            INTEGER,
    error_detail      TEXT             -- JSON array if errors > 0
);

CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);
```

### 5.4 Jellyfin API surface used

For fields/operations not well-covered by `jellyfin-apiclient-python`, the service calls the REST API directly using `requests`. Known endpoints required:

| Purpose | Endpoint |
|---|---|
| List items in library | `GET /Items?ParentId={libId}&Recursive=true&Fields=Path,People,Studios` |
| Get single item | `GET /Items/{itemId}?Fields=People,Studios,LockedFields` |
| Update item metadata | `POST /Items/{itemId}` (full item body) |
| List People | `GET /Persons?searchTerm={name}` |
| Create Person (implicit) | People array on item update creates missing People |
| List collections | `GET /Items?IncludeItemTypes=BoxSet&Recursive=true` |
| Create collection | `POST /Collections?name={name}&ids={id1,id2}` |
| Add to collection | `POST /Collections/{id}/Items?ids={id1,id2}` |
| Remove from collection | `DELETE /Collections/{id}/Items?ids={id1}` |
| Delete collection | `DELETE /Items/{id}` |
| Trigger library scan | `POST /Library/Refresh` |
| Get user data for item | `GET /Users/{userId}/Items/{itemId}?Fields=UserData` |
| Mark item played | `POST /Users/{userId}/PlayedItems/{itemId}` |
| Update item user data | `POST /Users/{userId}/Items/{itemId}/UserData` (body: `{PlayCount, LastPlayedDate, Rating}`) |
| Merge items as alternate versions | `POST /Videos/MergeVersions?ids={primary},{alt1},{alt2}` |
| Unmerge alternate versions | `DELETE /Videos/{primaryId}/AlternateSources` |
| Get media sources on a merged item | `GET /Items/{itemId}?Fields=MediaSources` (returned as `MediaSources` array on item) |

All requests include `X-Emby-Token: {api_key}` header.

### 5.5 Plex API surface used

Via `plexapi`:

- `PlexServer(base_url, token)` — owner-token connection
- `plex.library.section(library_name)` → the configured library
- `section.all()` → list of items
- `section.collections()` → list of collections
- `item.reload()` → force fresh fetch
- `item.studio`, `item.writers`, `item.directors`, `item.collections`, `item.media[].parts[].file`
- **Multi-file/merged items:** `[part.file for m in item.media for part in m.parts]` enumerates all files under a Plex item, whether merged from multiple sources or a single source with multiple parts
- **Plex primary file:** `item.media[0].parts[0].file` is the primary version (what Plex uses for default playback)
- `item.isWatched`, `item.viewCount`, `item.userRating`, `item.lastViewedAt` (owner-account values)
- `collection.items()` → resolves both manual and smart collections
- Per-account token support: `PlexServer(base_url, account_specific_token)` for each mapped Plex account, reading the same item via its `ratingKey` to get that account's `isWatched`/`viewCount`/`userRating`/`lastViewedAt`

---

## 6. Deployment

### 6.1 Docker Compose

The stack is deployed as a standalone compose project on the TrueNAS host, separate from the Plex and Jellyfin app-catalog apps.

**File:** `/mnt/tank/apps/plex-jellyfin-sync/docker-compose.yml` (illustrative structure; no YAML literal shown here — this is the spec document)

Structure:
- Single service `sync`
- Image built from local `Dockerfile` (or pinned tag from a user-owned registry)
- Base image: `python:3.12-slim`
- Exposes container port 8089; published on host port of user's choice (suggest 8089)
- Volumes:
  - `./config:/config:ro` — config file
  - `./state:/state` — SQLite database persistence
  - `/mnt/tank/media/othervideo:/media/othervideo:ro` — media (read-only, path validation only; no file IO)
- Environment variables for secrets (**tokens embedded directly in compose file**, per user requirement):
  - `PLEX_TOKEN`
  - `JELLYFIN_API_KEY`
  - `WEBHOOK_SHARED_SECRET` (optional)
- Restart policy: `unless-stopped`
- Network: attached to the same user-defined bridge network as Plex and Jellyfin containers so the sync service can reach them by service name. If Plex and Jellyfin are on the TrueNAS host network and not in a shared user network, `host.docker.internal` or the TrueNAS host IP must be used in `base_url` values.

**Tokens in YAML:** per user specification, `PLEX_TOKEN` and `JELLYFIN_API_KEY` are written as literal values in the compose file's `environment:` block. The user is responsible for file permissions on the compose file and for not committing it to any repository. The config file path is also configurable so the user may choose to put tokens either in `config.yaml` or in compose `environment`. Recommendation: compose `environment` block, with `config.yaml` reading them via `${VAR}` substitution at container start — but for absolute clarity with the "no .env" constraint, tokens are present as literals in `docker-compose.yml`.

### 6.2 Dockerfile structure (spec only)

- `FROM python:3.12-slim`
- Install system deps: `tini` (for signal handling)
- Copy `pyproject.toml`/`requirements.txt`, install:
  - `plexapi`
  - `jellyfin-apiclient-python`
  - `requests`
  - `fastapi`
  - `uvicorn[standard]`
  - `pydantic`
  - `pyyaml`
  - `structlog`
- Copy source tree
- Non-root user `sync` with UID/GID matching TrueNAS user owning the state volume
- `ENTRYPOINT ["tini", "--"]`
- `CMD ["python", "-m", "plex_jellyfin_sync"]`

### 6.3 TrueNAS Jellyfin app configuration prerequisites

These steps must be performed **before** the sync stack is started for the first time.

1. **Install the Jellyfin app** from the TrueNAS SCALE app catalog (same catalog that provided Plex).
2. **Storage configuration:** when configuring the Jellyfin app, add a host path volume that mounts the same ZFS dataset Plex uses for its "Other Video" library. The host path must be identical; the in-container path should also match Plex's in-container path for this library. For example:
   - Plex app mounts `/mnt/tank/media/othervideo` → `/media/othervideo` inside Plex container
   - Jellyfin app must mount `/mnt/tank/media/othervideo` → `/media/othervideo` inside Jellyfin container
   - The sync container must mount `/mnt/tank/media/othervideo` → `/media/othervideo`
   - This makes the path from Plex's `item.media[].parts[].file` a literal match for Jellyfin's `Path` field without any rewrite rules.
   - If identical in-container paths are not possible, configure `path_mapping.rules` in `config.yaml` to translate between the two.
3. **Create the Jellyfin library:**
   - In Jellyfin → Dashboard → Libraries → Add Media Library
   - Content type: **Mixed Content** (equivalent to Plex's "Other Video"; `Movies` is also acceptable and slightly better supported, but loses some item-type flexibility)
   - Display name: `Other Video` (or whatever matches `jellyfin.library_name` in config)
   - Folder: the in-container mount path
   - Metadata downloaders: **disable all** (leave unchecked)
   - Image fetchers: user's preference; recommended to disable to avoid overwriting sync-managed artwork in future versions
   - Save and let Jellyfin perform the initial library scan
4. **Wait for scan completion.** Confirm items are present and paths visible on each item (Dashboard → Libraries → item details).
5. **Generate a Jellyfin API key:**
   - Dashboard → Advanced → API Keys → "+"
   - App name: `plex-jellyfin-sync`
   - Copy the generated key into the compose file's `JELLYFIN_API_KEY` and the `jellyfin.api_key` field in `config.yaml`
6. **Optional: capture the admin user ID for troubleshooting only:**
   - Dashboard → Users → click the admin user
   - The URL contains `userId=<guid>`
   - The current implementation does not require `jellyfin.user_id`, so this can be omitted from `config.yaml`
7. **Configure the Plex webhook:**
   - Plex web UI → Account Settings → Webhooks → Add
   - URL: `http://<truenas-host>:<published-port>/webhook/plex`
   - Save
8. **First-run bootstrap:**
   - Start the sync stack: `docker compose up -d`
   - Trigger initial full sync: `curl -X POST http://<truenas-host>:<published-port>/trigger/full-sync`
   - Monitor logs via Dozzle; initial sync will take proportional time to library size

### 6.4 Path mapping guidance

- **Preferred:** identical in-container paths on all three containers (Plex, Jellyfin, sync). No `path_mapping` rules needed.
- **If Plex app and Jellyfin app use different default container paths:** most TrueNAS app templates let you override the in-container mount path. Override both to the same value.
- **If override is not possible:** use `path_mapping.rules` in config. Example:
  ```yaml
  path_mapping:
    rules:
      - plex_prefix: "/data/othervideo"
        jellyfin_prefix: "/media/othervideo"
  ```
  The mapper applies the longest-matching prefix from `plex_prefix` and substitutes `jellyfin_prefix` when comparing or looking up Jellyfin items by path.

### 6.5 Networking

Three supported topologies, user picks based on how TrueNAS has deployed Plex and Jellyfin:

- **All three on host network:** use `http://<truenas-host>:32400` and `http://<truenas-host>:8096` in config
- **All three on a user-defined bridge:** use service names `http://plex:32400` and `http://jellyfin:8096`
- **Mixed (typical TrueNAS apps are on their own networks):** use the TrueNAS host IP and published ports; ensure the sync container can reach them

---

## 7. Testing

### 7.1 Unit tests

All unit tests run in isolation with no network calls. Both `plexapi` and the Jellyfin client are mocked. SQLite uses in-memory databases per test.

#### 7.1.1 `config` module

- Loads a valid `config.yaml` and returns a populated typed config object
- Raises a clear error when required fields are missing (`plex.base_url`, `plex.token`, `plex.library_name`, equivalents for Jellyfin)
- Environment variable substitution works in config values (e.g. `token: "${PLEX_TOKEN}"`)
- Defaults are applied correctly (`debounce_seconds=900`, `full_sync_debounce_seconds=30`, `refresh_timeout_seconds=120`, `max_requeue_count=3`, etc.)
- Invalid types (string where int expected) produce validation errors
- Path mapping rules with overlapping prefixes are ordered by prefix length descending

#### 7.1.2 `state` module

- Schema is created on first open of a fresh DB
- Schema migrations apply in order when upgrading from an older version
- `item_map` CRUD: insert, lookup by `plex_rating_key`, update, delete
- `media_source_map` CRUD: insert, lookup by `path`, lookup by `plex_rating_key` (multi-row return for merged items), lookup by `jellyfin_source_id`, delete
- CASCADE delete: removing an `item_map` row removes all its `media_source_map` rows and `user_data_map` rows
- `collection_map` CRUD
- `person_cache` CRUD and cache hit/miss behaviour
- `user_data_map` CRUD keyed on (ratingKey, jellyfin_user_id) composite
- `sync_log` records a run from start to completion including error arrays and merge counts
- Concurrent writes from within the same process are serialised (SQLite WAL mode)
- Unique constraint violations surface as typed exceptions
- Query helper: `get_primary_and_sources(ratingKey)` returns the Jellyfin primary id plus the full list of source paths and source ids for a merged item

#### 7.1.3 `path_mapper` module

- No-op when `rules` is empty (input equals output)
- Single-rule mapping translates paths correctly
- Longest-prefix match wins when multiple rules could apply
- Paths not matching any rule are returned unchanged
- Reverse mapping (Jellyfin path → Plex path) produces the correct Plex path
- Windows-style paths are rejected with a clear validation error
- Trailing slashes in prefixes are handled

#### 7.1.4 `mapper` module

Pure-function tests using synthetic Plex item fixtures:

- A Plex item with studio "Studio A" produces a Jellyfin intent with `Studios=[{Name: "Studio A"}]`
- A Plex item with writers `[Writer(tag="Alice"), Writer(tag="Bob")]` produces Jellyfin People with `Type=Actor` for both names (writers-as-actors remap)
- A Plex item with directors `[Director(tag="Carol")]` produces Jellyfin People with `Type=Director`
- An item with both writers and directors produces a merged People list with correct types
- An item with no studio produces `Studios=[]` (not `None`)
- Duplicate person names across writers and directors produce two entries (one Actor, one Director) with the same name
- Collections on an item are extracted as a list of names

#### 7.1.5 `user_data_merger` module

Pure-function tests covering the non-destructive merge rules (§3.5.3):

- **Watched promotion:** Plex watched + Jellyfin unwatched → result is watched
- **Watched preservation:** Plex unwatched + Jellyfin watched → result is watched (no regression)
- **Watched agreement:** both watched → no write flag set
- **Unwatched agreement:** both unwatched → no write flag set
- **Play count promotion:** Plex viewCount 5, Jellyfin playCount 2 → result is 5
- **Play count preservation:** Plex viewCount 2, Jellyfin playCount 5 → no change, no write flag
- **Play count equal:** both 3 → no write flag
- **Play count zero vs zero:** no write flag
- **Rating update:** Plex rating 8.0, Jellyfin rating 6.0 → write 8.0
- **Rating cleared in Plex:** Plex rating None, Jellyfin rating 7.0 → no change (Plex None is "no opinion")
- **Rating equal:** Plex 7.5, Jellyfin 7.5 → no write flag
- **Last-played merge:** Plex 2025-04-01, Jellyfin 2025-04-05 → result 2025-04-05 (Jellyfin wins)
- **Last-played promotion:** Plex 2025-04-10, Jellyfin 2025-04-05 → result 2025-04-10
- **Last-played Plex-only:** Jellyfin None → result equals Plex value
- **Last-played Jellyfin-only:** Plex None → result equals Jellyfin value (no write flag for that field)
- **Combined diff:** all four fields differ; returned struct contains correct merged values and a write flag indicating which fields changed
- **All fields agree:** returned struct has no write flag; sync engine skips the API call

#### 7.1.6 `merge_planner` module

Pure-function tests covering merge planning logic (§3.6). Inputs are representations of Plex item file lists and current Jellyfin grouping state; outputs are operation lists (`merge`, `unmerge`, `remerge`, `noop`).

- **Single-file Plex item, single Jellyfin item:** plan returns `noop`
- **Single-file Plex item, Jellyfin item is part of a group:** plan returns `unmerge(<group_primary>)`
- **Multi-file Plex item, no Jellyfin grouping:** plan returns `merge(primary=<p>, alts=[<a1>,<a2>])` where primary matches `item.media[0].parts[0].file`
- **Multi-file Plex item, Jellyfin already grouped identically:** plan returns `noop`
- **Multi-file Plex item, Jellyfin grouped with wrong primary:** plan returns `remerge(old_primary=<old>, new_primary=<new>, alts=[...])` → expands to one unmerge + one merge
- **Multi-file Plex item, Jellyfin grouped with extra member:** plan returns `remerge` with correct member list
- **Multi-file Plex item, Jellyfin grouped with missing member:** plan returns `remerge` with the correct (larger) member list
- **Multi-file Plex item, Jellyfin items not yet scanned (some paths unresolved):** plan returns `defer` with list of unresolved paths
- **Multi-file Plex item, all Jellyfin items unresolved:** plan returns `defer`
- **Primary selection stability:** given the same Plex item file ordering, repeated planning returns the same primary id
- **Primary selection with path mapping:** Plex primary path after path_mapper translation matches the Jellyfin item's Path field
- **Unmerge detection from prior state:** if `item_map` previously recorded `is_merged=1` and the current Plex item has only one file, plan returns `unmerge`

#### 7.1.7 `diff` module

- Given equal intended and current state, produces empty change set
- Field change in `Studios` produces an `update` operation
- Adding one Actor produces a People-add operation
- Removing one Actor produces a People-remove operation
- Replacing Actor Alice with Actor Zoe produces both a remove and an add
- Director changes don't affect Actor entries and vice versa
- Content hash computation is stable (same input → same hash; input order in lists of people must not change hash if the set is equal)
- Collection membership diff: items in Plex set but not Jellyfin set → add; items in Jellyfin but not Plex → remove; items in both → no-op

#### 7.1.8 `plex_client` module

With `plexapi` mocked at the HTTP layer using recorded responses (VCR-style or hand-built fixtures):

- Connects and authenticates
- Fetches items from the configured library
- Skips libraries of other names
- Retrieves an item by `ratingKey`
- Enumerates collections
- Resolves `collection.items()` for both manual and smart collections
- Raises a typed error on authentication failure
- Handles an item with no writers/directors/studio (empty lists, None studio)
- Reads `isWatched`, `viewCount`, `userRating`, `lastViewedAt` for the owner account
- Reads per-account user data for a non-owner account using an alternate token
- Missing or expired alternate token raises a typed error without affecting owner-account reads
- Enumerates all file paths for a multi-file/merged item via `[part.file for m in item.media for part in m.parts]`
- Correctly identifies the primary file as `item.media[0].parts[0].file`

#### 7.1.9 `jellyfin_client` module

With the Jellyfin API mocked via `responses` or `httpx.MockTransport`:

- Authenticates with API key
- Fetches items from configured library
- Gets single item by ID with expected fields
- Posts an item update with `Studios`, `People`, `LockedFields`
- Searches Persons by name
- Upserts Person: returns existing ID if name matches, creates new if not, caches result
- Creates a new BoxSet collection
- Adds items to a BoxSet
- Removes items from a BoxSet
- Deletes an empty BoxSet
- Triggers a library refresh
- Propagates HTTP errors as typed exceptions
- Retries on 5xx with exponential backoff up to configurable max
- Reads per-user UserData (PlayCount, Played, Rating, LastPlayedDate) for a given userId/itemId
- Writes UserData via POST with correct payload shape
- Marks item played via the dedicated PlayedItems endpoint
- Never calls the unplay endpoint (`DELETE /Users/{id}/PlayedItems/{id}`) in v1 (safety: verified no code path exists)
- Calls `POST /Videos/MergeVersions` with correct ids list and primary first
- Calls `DELETE /Videos/{id}/AlternateSources` to unmerge
- Reads `MediaSources` array from an item to detect current grouping

#### 7.1.10 `debounce_queue` module

- Single event for key K dispatches after debounce window
- Second event for K during window resets the window
- Event for K' during K's window does not affect K's window
- `clear_all()` cancels pending events
- Full-sync events use the shorter debounce
- Multiple full-sync events coalesce to one dispatch
- Queue survives exceptions in dispatched handlers (handler failure does not kill the queue worker)
- **Manual-trigger preemption when idle:** calling `submit_manual_full_sync()` while no sync is running clears all pending per-item and full-sync debounced events and dispatches the full sync immediately
- **Manual-trigger preemption when busy:** calling `submit_manual_full_sync()` while a sync is in progress queues exactly one full sync to run next; all pending debounced events present when that full sync begins are cleared at that moment (not at submission time)
- **Manual-trigger coalescing:** multiple manual triggers submitted while a full sync is already queued result in exactly one full sync
- **Per-item and user-data debounce separation:** a `library.new` event and a `media.scrobble` event for the same ratingKey have independent debounce windows (15 min vs 60 s) and can both fire
- **User-data debounce window:** 60 s default; resets on additional user-data events for the same (ratingKey, account) pair
- **Requeue support:** submitting an event with `requeue_count=N` restarts the debounce window; at `requeue_count >= max_requeue_count` the event is dropped and logged

#### 7.1.11 `webhook_server` module

Using FastAPI TestClient:

- POST to `/webhook/plex` with a `library.new` payload enqueues a per-item sync with 15-minute debounce
- POST with `media.scrobble` payload from a mapped account enqueues a per-item user-data sync with 60-second debounce, scoped to that account's Jellyfin user
- POST with `media.scrobble` from an unmapped Plex account is accepted (200) but enqueues nothing
- POST with `media.rate` from a mapped account enqueues a user-data sync
- POST with `media.play` event returns 200 and enqueues nothing
- POST with missing or wrong shared secret (if configured) returns 401
- POST with malformed JSON returns 400
- POST to `/trigger/full-sync` enqueues a full sync with the new preemption semantics and returns 202 with job ID
- GET to `/trigger/status/{id}` returns the correct job state
- GET on unknown job ID returns 404
- `/healthz` returns 200 when state DB is reachable
- `/healthz` returns 503 when state DB is unreachable
- `/readyz` returns 200 only when both Plex and Jellyfin are reachable

#### 7.1.12 `sync_engine` module (unit-level, heavy mocking)

- Full sync flow: fetches library, fetches state, computes diff, applies writes, updates state — all components asserted via mock call expectations
- Full sync iterates all mapped users for user-data sync
- Full sync triggers `POST /Library/Refresh` at start and waits up to `refresh_timeout_seconds` before proceeding
- Single-item metadata sync flow: fetches one Plex item, looks up Jellyfin ID in state, applies diff
- Single-item user-data sync flow: reads only user-data fields from Plex for a specific account, reads current Jellyfin user-data, invokes `user_data_merger`, writes only if merger indicates a change
- Single-item sync for an unknown Plex `ratingKey` falls back to path-based lookup in Jellyfin
- Item deleted in Plex: removes item_map row, media_source_map rows, and user_data_map rows via CASCADE after confirming absence
- Collection created in Plex: creates Jellyfin collection, adds members
- Collection deleted in Plex: deletes Jellyfin collection
- Collection membership change: adds/removes applied via correct endpoints
- On Jellyfin write failure: sync_log records error, state is not updated for that item, next sync retries
- On user-data write failure: item metadata state is preserved (user-data failure does not roll back item sync)
- **Multi-file Plex item, initial merge:** invokes `merge_planner`, calls `MergeVersions` with the planner's output, writes metadata to the primary, records `is_merged=1` and multiple `media_source_map` rows
- **Multi-file Plex item, already correctly merged:** no merge API call, only metadata diff applied
- **Multi-file Plex item, Jellyfin grouping disagrees with Plex:** unmerges the incorrect grouping and remerges per Plex
- **Plex unmerge propagation:** when a previously-merged item is now single-file in Plex, calls `DELETE /Videos/{id}/AlternateSources` and updates `is_merged=0`
- **Deferred merge on full sync:** when a Plex item has a path with no resolvable Jellyfin item at refresh-wait timeout, logs and skips (does not requeue in full sync)
- **Deferred merge on per-item sync:** when paths are unresolvable, calls `POST /Library/Refresh`, requeues the event with incremented `requeue_count`, does not apply the merge or metadata write
- **Requeue exhaustion:** when `requeue_count >= max_requeue_count`, logs a hard error, drops the event, updates sync_log
- **Metadata writes on merged items target primary only:** for a 3-file merged item, the POST /Items/{id} call is made exactly once and to the primary's id
- **UserData writes on merged items target primary only:** UserData is written to the primary id; non-primary source ids are never used for UserData calls

### 7.2 Functional tests

Functional tests exercise real code paths against a real-enough substrate without touching the user's production Plex or Jellyfin.

#### 7.2.1 What is reasonable without disruption

Because the user does not have a staging Plex or Jellyfin, and production Plex must not be written to, the following boundaries apply:

- **Plex:** read-only functional tests against production Plex are acceptable but opt-in (flag-gated). They verify the `plex_client` module works end-to-end against the real library. No writes.
- **Jellyfin:** write-capable functional tests must target a **separate, dedicated test library** on the production Jellyfin server. Items in this test library must be short sample files (or a subset of actual content hardlinked into a test folder; no duplication of data). The test library name and path are configured separately and never overlap with the production Jellyfin library the sync service manages.

#### 7.2.2 In-process functional tests (no live servers)

These use a bundled docker-compose-based test harness that starts a throwaway Jellyfin container with a tiny sample library:

- **Test harness:** `docker-compose.test.yml` starts a Jellyfin instance pointed at `./tests/fixtures/media/` (three small sample video files under 1 MB each, generated by `ffmpeg testsrc`).
- Plex is **not** started in the test harness; it is mocked in Python using recorded API responses.
- Tests run with `pytest` and `testcontainers` (or raw docker-compose lifecycle via fixtures).

Functional test cases:

1. **Full-sync from empty Jellyfin** — mock Plex with 3 items, each with studio/writers/directors and one collection. Assert all three items in the real test Jellyfin have correct Studios and People, and the collection exists with all three as members.
2. **Idempotency** — run the same sync twice; second run produces zero writes (verified via Jellyfin request log).
3. **Studio change** — change studio in mock Plex, re-sync, verify update in real Jellyfin.
4. **Add writer** — add a writer name in mock Plex, re-sync, verify a new Person with `Type=Actor` appears on the item in real Jellyfin and the Person entity was created server-side.
5. **Remove writer** — inverse of above; verify the Person is removed from the item (the Person entity itself may remain; we only manage the item's People list).
6. **Collection creation** — add a new collection in mock Plex; verify the BoxSet is created in Jellyfin with correct members.
7. **Collection membership change** — move an item between two collections; verify both BoxSets' memberships are updated.
8. **Collection deletion** — remove a collection from mock Plex; verify the BoxSet is deleted in Jellyfin.
9. **Smart collection materialisation** — the mock Plex fixture includes a smart collection whose `items()` returns a specific set; verify the Jellyfin BoxSet has exactly that set.
10. **Field lock** — after sync, verify the Jellyfin item has `LockedFields` set to include `Studios` and `Cast`.
11. **Watched state promotion** — mock Plex item watched (viewCount=1), Jellyfin unwatched. Sync. Verify Jellyfin UserData.Played=true, PlayCount=1.
12. **Watched state preservation** — mock Plex item unwatched, Jellyfin watched (PlayCount=3). Sync. Verify Jellyfin remains Played=true with PlayCount=3 (no regression).
13. **Play count promotion** — mock Plex viewCount=5, Jellyfin PlayCount=2. Verify Jellyfin updates to 5.
14. **Play count preservation** — mock Plex viewCount=2, Jellyfin PlayCount=5. Verify Jellyfin remains 5.
15. **Rating update** — mock Plex userRating=8.0, Jellyfin Rating=6.0. Verify Jellyfin updates to 8.0.
16. **Rating cleared in Plex** — mock Plex userRating=None, Jellyfin Rating=7.0. Verify Jellyfin remains 7.0.
17. **Last-played monotonicity** — mock Plex lastViewedAt earlier than Jellyfin LastPlayedDate. Verify Jellyfin LastPlayedDate does not regress.
18. **Manual trigger endpoint (idle)** — POST to `/trigger/full-sync` while no sync is running; verify sync starts immediately and job status reflects progress.
19. **Manual trigger clears debounce queue** — enqueue 5 per-item webhook events (15-min debounce), immediately POST `/trigger/full-sync`. Verify only the full sync runs and the 5 per-item events are discarded.
20. **Manual trigger during active sync** — start a full sync, immediately POST a second `/trigger/full-sync` while the first is running. Verify the second runs exactly once after the first completes, and any events arriving during the first sync's execution are cleared at the moment the second starts.
21. **Webhook endpoint** — POST a Plex `library.new` webhook payload; verify 15-minute debounce window applies and sync dispatches after it.
22. **Debounce reset** — POST two `library.new` webhooks 5 minutes apart (window is 15 min); verify only one sync runs, 15 minutes after the second webhook.
23. **User-data webhook** — POST a `media.scrobble` event for a mapped Plex account; verify a user-data sync dispatches after 60 seconds and writes to the correct Jellyfin user.
24. **Unmapped-account webhook drop** — POST a `media.scrobble` event for an unmapped Plex account; verify no sync is enqueued.
25. **Restart recovery** — run sync, stop container mid-sync (using docker stop), restart, verify state DB is consistent and a fresh full sync completes successfully.
26. **Path mapping** — configure non-identity path rules; verify items are correctly matched despite path difference.
27. **Multi-file merged item — initial merge** — fixture has a mock Plex item with 3 files. Jellyfin test library contains 3 separate items pre-sync. Run sync. Verify Jellyfin exposes 1 top-level item with 3 `MediaSources`, primary path matches `item.media[0].parts[0].file`, and metadata is written to the primary.
28. **Multi-file merged item — idempotency** — repeat test 27's sync; verify no `MergeVersions` or `AlternateSources` calls on second run.
29. **Multi-file merged item — metadata update** — change studio on the merged item in mock Plex; verify update is written to the primary only (no writes to non-primary source ids).
30. **Multi-file merged item — watched state** — mark item watched in Jellyfin (via API setup), then configure mock Plex with unwatched + viewCount=0. Sync. Verify Jellyfin remains watched (per §3.5.3).
31. **Plex unmerge propagation** — start with a merged item (test 27's state), then change mock Plex to split it into separate items (single-file each). Run sync. Verify Jellyfin unmerges; each file becomes a top-level item again; metadata writes to each individually.
32. **Jellyfin grouping disagrees with Plex** — pre-merge 2 of 3 Jellyfin items manually via API, then sync with mock Plex having all 3 merged. Verify the incorrect 2-item group is unmerged and replaced with the correct 3-item group, primary matching Plex.
33. **Deferred merge via requeue** — mock Plex item references 3 files but Jellyfin library contains only 2 of them. Submit a `library.new` webhook event. Verify a `POST /Library/Refresh` is called and the event is requeued in the debounce queue with `requeue_count=1`. After simulating the third file appearing in Jellyfin, the requeued event dispatches and completes the merge.
34. **Requeue exhaustion** — same setup as test 33, but the missing Jellyfin item never appears. Verify the event is dropped after `max_requeue_count` attempts and a hard error appears in `sync_log`.

#### 7.2.3 Opt-in production-read tests

Behind `--live-plex` pytest flag, with read-only Plex access:

1. Connect to production Plex with configured token; library named in config is found.
2. Enumerate all items in the library; count matches what the user sees in the UI.
3. For a user-specified `ratingKey`, fetched studio / writers / directors / collections match expectations.
4. Enumerate collections; known collection names are present.
5. Smart collection membership via `collection.items()` matches a snapshot provided by the user.
6. For a user-specified merged `ratingKey`, `item.media` has the expected number of entries and `item.media[0].parts[0].file` matches the expected primary path.

No Jellyfin live-production tests are defined. Write tests against real Jellyfin are only acceptable via a dedicated test library as described in §7.2.1.

### 7.3 Test infrastructure

- `pytest` with `pytest-asyncio` for async tests
- `responses` library for mocking `requests`-based HTTP
- `testcontainers-python` or raw docker-compose fixtures for the Jellyfin test container
- Fixtures directory:
  - `tests/fixtures/plex/` — JSON snapshots of `plexapi` API responses, including at least one single-file item, one manually-merged multi-file item, and one smart collection
  - `tests/fixtures/media/` — tiny sample videos under 1 MB each, generated by `ffmpeg testsrc`, including at least 3 files used as a "merged" set in test 27
  - `tests/fixtures/jellyfin/` — recorded Jellyfin API response shapes for `MediaSources`, `UserData`, `MergeVersions`, and `AlternateSources` endpoints, pinned to the tested Jellyfin version
- A pinned Jellyfin version (documented in repo README) is used for the test container to ensure fixture parity
- Coverage target: ≥ 85% line coverage on non-I/O modules (`config`, `state`, `path_mapper`, `mapper`, `user_data_merger`, `merge_planner`, `diff`, `debounce_queue`). Client modules are tested via integration rather than coverage targets.

---

## 8. Observability

### 8.1 Logging

- Structured logs via `structlog`, JSON format by default, text format optional for dev
- All logs go to stdout, captured by Docker and visible via Dozzle
- Log fields include: `event`, `plex_rating_key`, `jellyfin_id`, `sync_scope`, `trigger`, `duration_ms`
- Additional fields for merge operations: `merge_op` (merge/unmerge/remerge/defer), `primary_path`, `alt_paths`, `requeue_count`
- Additional fields for user-data operations: `jellyfin_user_id`, `plex_account`, `userdata_changes` (list of which fields changed)
- Error logs include full exception traceback
- Warning-level log is emitted whenever a remerge overwrites an existing Jellyfin grouping (discoverable signal per §3.6.5)
- Sync summary log at end of each run: items examined, items updated, user-data updates, merges applied, unmerges applied, requeued events, errors

### 8.2 Metrics

No Prometheus endpoint in v1. The `sync_log` table is queryable for historical runs via an admin endpoint:

- `GET /admin/sync-log?limit=50` — returns recent sync runs as JSON
- `GET /admin/stats` — returns counts (items tracked, collections tracked, last successful full sync timestamp)

### 8.3 Health

- `GET /healthz` — liveness (process up)
- `GET /readyz` — readiness (can reach Plex and Jellyfin, state DB is healthy)

---

## 9. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| `jellyfin-apiclient-python` is incomplete for People, Collection, and LockedFields operations | Fall back to direct `requests` calls for uncovered endpoints; document which endpoints need this in code |
| Plex webhooks don't fire for metadata edits | Manual trigger endpoint; user convention of hitting the trigger after edits |
| Path mismatch between Plex and Jellyfin | Mandate identical in-container paths in setup; provide `path_mapping.rules` as escape hatch |
| Jellyfin library scan overwrites synced fields | Set `LockedFields` after each write |
| Plex API rate limiting during full sync | Serial item processing with small inter-request delay configurable; `plexapi` handles basic retry |
| Per-account Plex tokens expire independently of the owner token | User-data sync for an affected account fails loudly in `sync_log`; item metadata sync continues; owner-token sync is unaffected; user is prompted to regenerate the account's token |
| Non-owner Plex user data access requires per-account tokens rather than the admin token | Documented in §3.5.2; config explicitly supports per-account tokens; if a user does not supply one, that account's user data is simply not synced |
| Jellyfin UserData endpoint shape changes between Jellyfin versions (historically volatile) | Pin a known-working Jellyfin version range in documentation; `jellyfin_client` tests use recorded fixtures that will fail loudly on response shape drift; allow easy override of endpoint paths via a config section if needed in future |
| Jellyfin `MergeVersions` / `AlternateSources` endpoints are under-documented and may change between versions | Pin and test against a specific Jellyfin version range; unit tests for `jellyfin_client` verify request/response shapes against fixtures; if endpoint behaviour drifts, tests fail before a live sync does |
| Destructive remerge overwrites a manually-created Jellyfin grouping | Documented as intentional behaviour per §3.6.5; a warning-level log entry is emitted each time a remerge is applied, making it discoverable via Dozzle |
| Plex returns an unexpected ordering for `item.media` (primary not at index 0) | `plexapi` preserves Plex server ordering; the primary file choice is defined as "whatever Plex says is first"; if Plex itself reorders, the sync follows — this matches user intent of "Plex is authoritative" |
| Library refresh timeout insufficient for very large libraries on first sync | Timeout is configurable; per-item debounce requeue provides a second chance; manual trigger can be re-issued after a long scan completes |
| Jellyfin 5xx during write | Exponential backoff retry; on exhaustion, log error and skip to next item; retry on next sync |
| Token leak via compose file | Document that compose file must not be committed; file perms `600`; user accepts this risk per requirement |
| Two real people with the same name collapse into one Jellyfin Person | Accepted limitation in v1; documented |
| Very large initial sync times out / is interrupted | Sync is resumable: each item is an independent transaction; on restart the sync engine picks up where it left off using `last_synced_at` staleness |
| Plex Pass not present (webhooks unavailable) | Manual trigger is the fallback; the system remains functional without webhooks, just with user-driven sync timing |

---

## 10. Out-of-band and Future Considerations

Items explicitly deferred from v1 but worth noting for v2+:

- Artwork sync (posters, background art, people images)
- Bidirectional sync or conflict detection when Jellyfin metadata is edited
- Playlist support (distinct from collections)
- Per-user resume positions (`viewOffset` / `PlaybackPositionTicks`)
- Scheduled polling as a defence-in-depth against missed webhooks
- Prometheus metrics endpoint
- Web UI for manual trigger, sync history, and configuration
- Support for multiple libraries
- Matching people by external IDs (TMDb, IMDb) rather than name string
- Artwork-managed fields in `LockedFields` once artwork sync is implemented
- Propagation of rating clears (treating Plex `userRating=None` as an intentional clear rather than "no opinion")
- Per-user manual trigger endpoint for refreshing user data for a specific account without a full resync
- UI affordance or Home Assistant integration for the manual trigger endpoint

---

## 11. Acceptance Criteria

v1 is complete when:

1. All unit tests in §7.1 pass with ≥ 85% line coverage on the listed pure modules (`config`, `state`, `path_mapper`, `mapper`, `user_data_merger`, `merge_planner`, `diff`, `debounce_queue`)
2. All in-process functional tests in §7.2.2 (tests 1–34) pass in CI using the docker-compose test harness
3. Opt-in live-read Plex tests pass when run against the user's production Plex, including at least one verified merged item
4. A first-run bootstrap against a freshly-configured Jellyfin test library correctly mirrors Studio, Writers-as-Actors, Directors, and collection membership for at least 10 sample items
5. A first-run bootstrap correctly materialises at least one multi-file Plex item as a Jellyfin alternate-version group with the primary matching Plex's primary
6. User data sync correctly propagates watched state, play count, and rating from a mapped Plex account to the mapped Jellyfin user under all six non-destructive merge cases (watched promotion/preservation/agreement, playcount promotion/preservation, rating update)
7. User data sync never regresses a Jellyfin item's watched state or play count regardless of Plex's state
8. Manual trigger and webhook endpoints behave per spec, including:
   - Manual trigger clears the debounce queue when starting
   - Manual trigger during active sync queues exactly one follow-up sync
   - Per-item debounce uses a 15-minute window
   - User-data debounce uses a 60-second window
   - Debounced events that cannot be resolved trigger a Jellyfin library refresh and are requeued (up to `max_requeue_count` times)
9. The stack survives a container restart mid-sync with no data corruption; resume behaviour picks up at the next `last_synced_at`-stale item
10. Plex unmerge is correctly propagated to Jellyfin on the next sync
11. A manual Jellyfin grouping that disagrees with Plex is corrected to match Plex on the next sync
12. Documentation in the repo includes: this spec, a README with setup steps, an operational runbook for common issues (wrong path mapping, expired tokens, stuck sync job, Jellyfin scan not keeping up with new files), and a documented pinned Jellyfin version range
