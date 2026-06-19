#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${HOII_REPO_SLUG:-tickernelz/hermes-openai-compatible-image}"
REF="${HOII_REF:-v0.1.1}"
SCRIPT_PATH="${0:-}"
SELF_DIR=""
TMP_DIR=""
BOOTSTRAP_DEPS=("PyYAML>=6" "rich>=13" "prompt_toolkit>=3")

if [ -n "$SCRIPT_PATH" ] && [ "$SCRIPT_PATH" != "bash" ] && [ "$SCRIPT_PATH" != "-" ] && [ -f "$SCRIPT_PATH" ]; then
  SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
fi

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

is_executable() {
  [ -n "${1:-}" ] && [ -x "$1" ] && [ -f "$1" ]
}

detect_hermes_home() {
  if [ -n "${HERMES_HOME:-}" ]; then
    printf '%s\n' "$HERMES_HOME"
    return 0
  fi
  printf '%s\n' "$HOME/.hermes"
}

select_python_bin() {
  local hermes_home="${1:-}"
  if [ -n "${HOII_HERMES_PYTHON:-}" ]; then
    printf '%s\n' "$HOII_HERMES_PYTHON"
    return 0
  fi
  if [ -n "${HERMES_PYTHON:-}" ]; then
    printf '%s\n' "$HERMES_PYTHON"
    return 0
  fi
  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "$PYTHON"
    return 0
  fi
  if is_executable "$hermes_home/hermes-agent/venv/bin/python"; then
    printf '%s\n' "$hermes_home/hermes-agent/venv/bin/python"
    return 0
  fi
  printf '%s\n' "python3"
}

needs_bootstrap_deps() {
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import yaml, rich, prompt_toolkit
PY
}

install_bootstrap_deps() {
  if [ "${HOII_SKIP_BOOTSTRAP_DEPS:-}" = "1" ]; then
    return 0
  fi
  if needs_bootstrap_deps; then
    return 0
  fi
  echo "Installing installer TUI dependencies with ${PYTHON_BIN}..." >&2
  if "$PYTHON_BIN" -m pip install --upgrade --no-cache-dir "${BOOTSTRAP_DEPS[@]}"; then
    return 0
  fi
  if "$PYTHON_BIN" -m pip install --user --upgrade --no-cache-dir "${BOOTSTRAP_DEPS[@]}"; then
    return 0
  fi
  echo "Error: failed to install TUI dependencies. Manual command:" >&2
  echo "  ${PYTHON_BIN} -m pip install 'PyYAML>=6' 'rich>=13' 'prompt_toolkit>=3'" >&2
  return 1
}

if [ -n "$SELF_DIR" ] && [ -d "$SELF_DIR/openai-compatible-image" ] && [ -f "$SELF_DIR/scripts/install.py" ]; then
  SRC_DIR="$SELF_DIR"
else
  TMP_DIR="$(mktemp -d)"
  ARCHIVE_URL="${HOII_ARCHIVE_URL:-https://github.com/${REPO_SLUG}/archive/${REF}.tar.gz}"
  echo "Downloading ${REPO_SLUG}@${REF}..." >&2
  curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_DIR" --strip-components=1
  SRC_DIR="$TMP_DIR"
fi

DETECTED_HERMES_HOME="$(detect_hermes_home)"
PYTHON_BIN="$(select_python_bin "$DETECTED_HERMES_HOME")"
export HOII_HERMES_PYTHON="$PYTHON_BIN"

install_bootstrap_deps

if [ -t 0 ]; then
  exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" --source-dir "$SRC_DIR" "$@"
elif [ -e /dev/tty ] && { : < /dev/tty; } 2>/dev/null; then
  exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" --source-dir "$SRC_DIR" "$@" < /dev/tty
else
  exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" --source-dir "$SRC_DIR" "$@"
fi
