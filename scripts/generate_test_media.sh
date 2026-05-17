#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEDIA_DIR="${ROOT_DIR}/tests/harness/media"

mkdir -p "${MEDIA_DIR}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required to generate test media" >&2
  exit 1
fi

generate_file() {
  local output="$1"
  ffmpeg -y \
    -f lavfi -i testsrc=size=320x240:rate=24 \
    -f lavfi -i sine=frequency=1000:sample_rate=48000 \
    -t 2 \
    -c:v libx264 \
    -pix_fmt yuv420p \
    -c:a aac \
    -shortest \
    "${output}" \
    >/dev/null 2>&1
}

generate_file "${MEDIA_DIR}/fixture-01.mp4"
generate_file "${MEDIA_DIR}/fixture-02.mp4"
generate_file "${MEDIA_DIR}/fixture-03.mp4"

echo "Generated sample media in ${MEDIA_DIR}"
