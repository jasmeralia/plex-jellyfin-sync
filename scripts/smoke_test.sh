#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.test.yml"
HARNESS_CONFIG_DIR="${ROOT_DIR}/tests/harness/config"
HARNESS_CONFIG="${HARNESS_CONFIG_DIR}/config.yaml"
HARNESS_ENV_FILE="${HARNESS_CONFIG_DIR}/jellyfin-bootstrap.env"

python_cmd() {
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    printf '%s\n' "${ROOT_DIR}/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  return 1
}

ensure_harness_config() {
  mkdir -p "${HARNESS_CONFIG_DIR}" "${ROOT_DIR}/tests/harness/state" "${ROOT_DIR}/tests/harness/jellyfin/config" "${ROOT_DIR}/tests/harness/jellyfin/cache"
  if [[ ! -f "${HARNESS_CONFIG}" ]]; then
    cp "${HARNESS_CONFIG_DIR}/config.test.example.yaml" "${HARNESS_CONFIG}"
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run_compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "${COMPOSE_FILE}" "$@"
  else
    docker-compose -f "${COMPOSE_FILE}" "$@"
  fi
}

ensure_harness_config

if [[ "${1:-}" == "--generate-media-only" ]]; then
  "${ROOT_DIR}/scripts/generate_test_media.sh"
  exit 0
fi

if ! command_exists docker; then
  echo "docker is required for the smoke harness" >&2
  exit 1
fi

if [[ ! -f "${ROOT_DIR}/tests/harness/media/fixture-01.mp4" ]]; then
  "${ROOT_DIR}/scripts/generate_test_media.sh"
fi

run_compose config >/dev/null

if [[ "${1:-}" == "--config-only" ]]; then
  echo "docker-compose.test.yml validated"
  exit 0
fi

PYTHON_BIN="$(python_cmd)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 or python is required for the smoke harness bootstrap" >&2
  exit 1
fi

run_compose up -d --build jellyfin

"${PYTHON_BIN}" -m plex_jellyfin_sync.harness_bootstrap \
  --base-url "http://localhost:18096" \
  --config "${HARNESS_CONFIG}" \
  --env-file "${HARNESS_ENV_FILE}" \
  --admin-username "${JF_BOOTSTRAP_ADMIN_USERNAME:-admin}" \
  --admin-password "${JF_BOOTSTRAP_ADMIN_PASSWORD:-plex-jellyfin-sync}" \
  --server-name "${JF_BOOTSTRAP_SERVER_NAME:-plex-jellyfin-sync-harness}" \
  --app-name "${JF_BOOTSTRAP_APP_NAME:-plex-jellyfin-sync}" \
  --library-name "${JF_BOOTSTRAP_LIBRARY_NAME:-Other Video}" \
  --media-path "${JF_BOOTSTRAP_MEDIA_PATH:-/media/othervideo}" \
  --collection-type "${JF_BOOTSTRAP_COLLECTION_TYPE:-mixed}" \
  --timeout-seconds "${JF_BOOTSTRAP_TIMEOUT_SECONDS:-120}"

set -a
source "${HARNESS_ENV_FILE}"
set +a

run_compose up -d --build sync

echo "Harness started."
echo "Jellyfin: http://localhost:18096"
echo "Sync service: http://localhost:18089"
echo "Bootstrap outputs:"
echo "1. Jellyfin startup wizard completed automatically."
echo "2. API key written to ${HARNESS_ENV_FILE}."
echo "3. Admin user id written into ${HARNESS_CONFIG}."
echo "Remaining manual step for true end-to-end sync:"
echo "4. Point the harness at a real Plex server/token if you want to validate live Plex->Jellyfin sync behavior."
