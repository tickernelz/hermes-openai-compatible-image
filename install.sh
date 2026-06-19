#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${HOII_REPO_SLUG:-tickernelz/hermes-openai-compatible-image}"
REF="${HOII_REF:-main}"
SELF_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR=""

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

if [ -d "$SELF_DIR/openai-compatible-image" ] && [ -f "$SELF_DIR/scripts/install.py" ]; then
  SRC_DIR="$SELF_DIR"
else
  TMP_DIR="$(mktemp -d)"
  ARCHIVE_URL="${HOII_ARCHIVE_URL:-https://github.com/${REPO_SLUG}/archive/refs/heads/${REF}.tar.gz}"
  echo "Downloading ${REPO_SLUG}@${REF}..." >&2
  curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_DIR" --strip-components=1
  SRC_DIR="$TMP_DIR"
fi

PYTHON_BIN="${PYTHON:-python3}"
exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" --source-dir "$SRC_DIR" "$@"
