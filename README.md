# plex-jellyfin-sync

One-way metadata and user-data sync from Plex to Jellyfin for a single library.

## Current Scope

The current implementation includes:

- config loading with environment-variable substitution
- FastAPI webhook and manual full-sync endpoints
- structured logging with secret redaction in startup logs
- debounce queue with manual-trigger priority
- automatic startup full sync
- SQLite-backed item, media-source, collection, user-data, and sync-log state
- Plex and Jellyfin client adapters
- item sync, merge planning, collection materialization, and per-user watched/play-count/rating sync
- stale item-map cleanup once items disappear from both Plex and Jellyfin; direct Jellyfin item deletion is still left to Jellyfin library scanning

The remaining gap versus `spec.md` is live-service validation against a real Plex server. The disposable Jellyfin harness bootstrap is automated, and refresh polling during full sync is implemented and covered by tests.

## Compatibility

The disposable harness in [docker-compose.test.yml](/home/morgan/git/plex-jellyfin-sync/docker-compose.test.yml) pins Jellyfin to `10.10.7`, and that `10.10.x` line is the documented target range for this repo right now.

Reason: several Jellyfin endpoints used here for user data, collection management, and version merging have historically changed shape across releases. If you move off `10.10.x`, re-run the smoke path and re-validate live sync behavior before treating the upgrade as safe.

## Configuration

Use [config/config.example.yaml](/home/morgan/git/plex-jellyfin-sync/config/config.example.yaml) as the starting point. The config supports `${VAR}` substitution, so the compose example injects secrets via environment variables rather than a `.env` file.

State path note:

- local runs default `state.sqlite_path` to `./state/sync.db`
- the Docker compose example sets `SYNC_SQLITE_PATH=/state/sync.db` so SQLite persists on the mounted `./state` volume

Required values:

- `PLEX_TOKEN`
- `JELLYFIN_API_KEY`
- each `user_mapping[].jellyfin_user_id` you want to sync

Useful Jellyfin runtime knobs:

- `plex.request_timeout_seconds`
- `plex.max_retries`
- `plex.retry_backoff_seconds`
- `jellyfin.request_timeout_seconds`
- `jellyfin.max_retries`
- `jellyfin.retry_backoff_seconds`
- `sync.merging.refresh_timeout_seconds`

Webhook auth note:

- `webhook.shared_secret` is optional
- blank or whitespace-only `WEBHOOK_SHARED_SECRET` values disable webhook secret validation

## Run Locally

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config/config.example.yaml config/config.yaml
.venv/bin/python -m plex_jellyfin_sync --config config/config.yaml
```

The server listens on `webhook.listen_port`.

## Run With Docker

```bash
cp docker-compose.example.yml docker-compose.yml
cp config/config.example.yaml config/config.yaml
docker compose up --build
```

Default mounts in the compose example:

- `./config:/config:ro`
- `./state:/state`
- `/mnt/tank/media/othervideo:/media/othervideo:ro`

Adjust the media mount, service URLs, and secrets for your host.

## Integration Harness

The repo now includes a disposable Jellyfin-based harness:

- [docker-compose.test.yml](/home/morgan/git/plex-jellyfin-sync/docker-compose.test.yml)
- [tests/harness/config/config.test.example.yaml](/home/morgan/git/plex-jellyfin-sync/tests/harness/config/config.test.example.yaml)
- [scripts/generate_test_media.sh](/home/morgan/git/plex-jellyfin-sync/scripts/generate_test_media.sh)
- [scripts/smoke_test.sh](/home/morgan/git/plex-jellyfin-sync/scripts/smoke_test.sh)

Useful harness commands:

```bash
bash scripts/generate_test_media.sh
bash scripts/smoke_test.sh --config-only
bash scripts/smoke_test.sh
```

Notes:

- `generate_test_media.sh` requires `ffmpeg`
- `smoke_test.sh` creates `tests/harness/config/config.yaml` from the example if it does not exist
- `smoke_test.sh` now auto-completes the Jellyfin startup wizard, generates a Jellyfin API key, writes it to `tests/harness/config/jellyfin-bootstrap.env`, and records the admin user id in `tests/harness/config/config.yaml`
- true end-to-end sync validation still requires a reachable Plex server and valid `PLEX_TOKEN`

## Operational Runbook

### Wrong path mapping

Symptoms:

- full sync logs unresolved item paths
- items exist in Jellyfin but are never matched by the sync service
- merge or collection updates work inconsistently because item lookup by path fails

Checks:

- confirm Plex and Jellyfin see the same media files at the same in-container path
- compare Plex file paths with Jellyfin item `Path` values
- review `path_mapping.rules` in `config.yaml` if the containers do not share identical mount points

Recovery:

1. Fix the container mounts or `path_mapping.rules` so Plex paths map to the exact Jellyfin paths.
2. Trigger a manual full sync with `POST /trigger/full-sync`.
3. Check `GET /admin/sync-log` for unresolved-path errors dropping to zero.

### Expired Plex or Jellyfin tokens

Symptoms:

- `GET /readyz` returns `503`
- sync log entries start failing with auth or unauthorized errors
- one mapped Plex user stops receiving watched/rating updates while metadata sync still works

Checks:

- verify `PLEX_TOKEN` still works against the Plex server
- verify `JELLYFIN_API_KEY` still works against the Jellyfin server
- for per-user sync, verify any `user_mapping[].plex_token` values are still valid

Recovery:

1. Replace the expired token in `config/config.yaml` or the injected environment variable.
2. Restart the container so the new credentials are loaded.
3. Re-run a manual full sync to backfill anything missed while auth was broken.

### Stuck sync job

Symptoms:

- `GET /trigger/status/{job_id}` stays in `queued` or `running`
- `/admin/sync-log` stops advancing
- webhook requests return accepted but no work appears to complete

Checks:

- inspect container logs for repeated retries, unresolved paths, or upstream timeouts
- verify `GET /healthz` and `GET /readyz`
- confirm the SQLite state path is writable and not full

Recovery:

1. If the process is unhealthy, restart the container.
2. If the job failed because Jellyfin had not scanned new files yet, wait for the library refresh to finish and trigger another full sync.
3. Review `/admin/sync-log?limit=50` after restart to confirm new runs are being recorded.

### Jellyfin scan not keeping up with new files

Symptoms:

- newly added Plex items trigger webhooks but do not resolve in Jellyfin immediately
- sync logs show unresolved paths followed by requeues

Checks:

- confirm Jellyfin has finished its library refresh
- check whether the library is large enough that `sync.merging.refresh_timeout_seconds` is too low

Recovery:

1. Increase `sync.merging.refresh_timeout_seconds` if Jellyfin regularly needs more time to index new media.
2. Let the requeue path retry the event, or trigger another manual full sync once the scan is complete.
3. If this is common on large imports, import media first, let Jellyfin finish scanning, then trigger the sync.

## Endpoints

- `POST /webhook/plex`
- `POST /trigger/full-sync`
- `GET /trigger/status/{job_id}` returns status plus result/error details when available
- `GET /admin/stats`
- `GET /admin/sync-log?limit=50`
- `GET /healthz`
- `GET /readyz`

## Verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall plex_jellyfin_sync tests
bash -n scripts/generate_test_media.sh
bash -n scripts/smoke_test.sh
bash scripts/smoke_test.sh --config-only
```

Opt-in live Plex validation:

```bash
.venv/bin/python -m pytest -q --live-plex --live-config config/config.yaml tests/test_live_plex.py
```

This is read-only against Plex. It is skipped unless `--live-plex` is provided.

Opt-in live Jellyfin validation:

```bash
.venv/bin/python -m pytest -q --live-jellyfin --live-config config/config.yaml tests/test_live_jellyfin.py
```

Opt-in live Jellyfin write validation:

```bash
.venv/bin/python -m pytest -q \
  --live-jellyfin \
  --live-jellyfin-writes \
  --live-config config/config.yaml \
  tests/test_live_jellyfin.py
```

Use the write-capable Jellyfin path only against a dedicated test library.

## Auto-Continue Scripts

If you use the local `codex` CLI with your normal Codex subscription/login, use:

```bash
python3 scripts/auto_continue_codex.py \
  --full-auto \
  --cd /home/morgan/git/plex-jellyfin-sync \
  "Continue autonomously until the task is done or blocked."
```

Notes:

- uses your existing local `codex` CLI session instead of the API
- starts with `codex exec`, then continues with `codex exec resume --last`
- if `--cd` points at a repo with `.venv`, the wrapper prepends `.venv/bin` to `PATH` and sets `VIRTUAL_ENV`, so Codex tool calls prefer the project virtualenv automatically
- enforces a strict `STATUS: CONTINUE|DONE|BLOCKED` protocol so the wrapper knows when to stop
- requires two consecutive `STATUS: DONE` replies by default; the first `DONE` triggers a verification turn instead of exiting immediately
- writes a transcript with `--transcript transcript.txt` if you want a log
- avoid running multiple unrelated codex sessions in the same workspace while the loop is active, because the wrapper resumes the most recent session

If you want the old behavior, pass `--done-confirmations 1`.

If you specifically want the API-key-based variant instead, use:

```bash
python3 scripts/auto_continue_responses.py \
  "Implement the task fully. Keep going until blocked or done."
```

That path requires `OPENAI_API_KEY` plus the Python `openai` package, and it now uses the same two-consecutive-`DONE` safeguard by default. Pass `--done-confirmations 1` if you want it to exit on the first `DONE`.
