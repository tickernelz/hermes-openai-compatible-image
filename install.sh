#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${HOII_REPO_SLUG:-tickernelz/hermes-openai-compatible-image}"
REF="${HOII_REF:-v0.1.1}"
SCRIPT_PATH="${0:-}"
SELF_DIR=""
TMP_DIR=""

if [ -n "$SCRIPT_PATH" ] && [ "$SCRIPT_PATH" != "bash" ] && [ "$SCRIPT_PATH" != "-" ] && [ -f "$SCRIPT_PATH" ]; then
  SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
fi

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

if [ -n "$SELF_DIR" ] && [ -d "$SELF_DIR/openai-compatible-image" ] && [ -f "$SELF_DIR/scripts/install.py" ]; then
  SRC_DIR="$SELF_DIR"
else
  TMP_DIR="$(mktemp -d)"
  ARCHIVE_URL="${HOII_ARCHIVE_URL:-https://github.com/${REPO_SLUG}/archive/${REF}.tar.gz}"
  echo "Downloading ${REPO_SLUG}@${REF}..." >&2
  curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_DIR" --strip-components=1
  SRC_DIR="$TMP_DIR"
fi

PYTHON_BIN="${PYTHON:-python3}"
exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" --source-dir "$SRC_DIR" "$@"
