#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${HOII_REPO_SLUG:-tickernelz/hermes-openai-compatible-image}"
REF="${HOII_REF:-v0.2.1}"
SOURCE_DIR="${HOII_SOURCE_DIR:-}"
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

arg_value() {
  local flag="$1"
  shift
  local arg
  while [ "$#" -gt 0 ]; do
    arg="$1"
    shift
    case "$arg" in
      "$flag")
        if [ "$#" -gt 0 ]; then
          printf '%s\n' "$1"
          return 0
        fi
        ;;
      "$flag"=*)
        printf '%s\n' "${arg#*=}"
        return 0
        ;;
    esac
  done
  return 1
}

find_hermes_in_path() {
  if [ -n "${HERMES_BIN:-}" ]; then
    printf '%s\n' "$HERMES_BIN"
    return 0
  fi
  command -v hermes 2>/dev/null || true
}

looks_like_python_executable() {
  local path_or_name="${1:-}"
  local base_name
  base_name="$(basename "$path_or_name")"
  [ "${base_name#python}" != "$base_name" ]
}

resolve_from_path() {
  local executable_name="${1:-}"
  [ -n "$executable_name" ] || return 1
  local old_ifs="$IFS"
  local path_dir
  IFS=':'
  for path_dir in $PATH; do
    [ -n "$path_dir" ] || continue
    if is_executable "$path_dir/$executable_name"; then
      IFS="$old_ifs"
      printf '%s\n' "$path_dir/$executable_name"
      return 0
    fi
  done
  IFS="$old_ifs"
  return 1
}

resolve_env_shebang_utility() {
  local target="${1:-}"
  read -r -a parts <<< "$target"
  [ "${#parts[@]}" -ge 2 ] || return 1
  [ "$(basename "${parts[0]}")" = "env" ] || return 1

  local index=1
  local token
  while [ "$index" -lt "${#parts[@]}" ]; do
    token="${parts[$index]}"
    if [ "$token" = "--" ]; then
      index=$((index + 1))
      break
    fi
    if [ "$token" = "-S" ]; then
      index=$((index + 1))
      break
    fi
    if [ "$token" = "-u" ] || [ "$token" = "-C" ] || [ "$token" = "-P" ]; then
      index=$((index + 2))
      continue
    fi
    case "$token" in
      -*) index=$((index + 1)); continue ;;
      *=*)
        case "$token" in
          /*) ;;
          *) index=$((index + 1)); continue ;;
        esac
        ;;
    esac
    break
  done

  [ "$index" -lt "${#parts[@]}" ] || return 1
  local executable_name="${parts[$index]}"
  looks_like_python_executable "$executable_name" || return 1
  if [ "${executable_name#/}" != "$executable_name" ] && is_executable "$executable_name"; then
    printf '%s\n' "$executable_name"
    return 0
  fi
  resolve_from_path "$executable_name"
}

infer_python_from_launcher_target() {
  local target="${1:-}"
  [ -n "$target" ] || return 1
  local real_target
  real_target="$(realpath "$target" 2>/dev/null || printf '%s\n' "$target")"
  local target_dir
  target_dir="$(dirname "$real_target")"
  if looks_like_python_executable "$real_target" && is_executable "$real_target"; then
    printf '%s\n' "$real_target"
    return 0
  fi
  if is_executable "$target_dir/python"; then
    printf '%s\n' "$target_dir/python"
    return 0
  fi
  if is_executable "$target_dir/python3"; then
    printf '%s\n' "$target_dir/python3"
    return 0
  fi
  return 1
}

infer_python_from_shell_launcher() {
  local real_bin="${1:-}"
  [ -n "$real_bin" ] || return 1
  local line after_exec command_token inferred
  while IFS= read -r line; do
    case "$line" in
      *exec*) ;;
      *) continue ;;
    esac
    after_exec="${line#*exec }"
    [ "$after_exec" != "$line" ] || continue
    command_token="${after_exec%%[[:space:]]*}"
    command_token="${command_token%\"}"
    command_token="${command_token#\"}"
    command_token="${command_token%\'}"
    command_token="${command_token#\'}"
    [ -n "$command_token" ] || continue
    case "$command_token" in
      \$*) continue ;;
    esac
    if [ "${command_token#/}" = "$command_token" ]; then
      command_token="$(resolve_from_path "$command_token" || true)"
    fi
    [ -n "$command_token" ] || continue
    inferred="$(infer_python_from_launcher_target "$command_token" || true)"
    if [ -n "$inferred" ]; then
      printf '%s\n' "$inferred"
      return 0
    fi
  done < "$real_bin"
  return 1
}

infer_python_from_hermes_bin() {
  local hermes_bin="${1:-}"
  [ -n "$hermes_bin" ] || return 1

  local real_bin
  real_bin="$(realpath "$hermes_bin" 2>/dev/null || printf '%s\n' "$hermes_bin")"
  local bin_dir
  bin_dir="$(dirname "$real_bin")"

  if is_executable "$bin_dir/python"; then
    printf '%s\n' "$bin_dir/python"
    return 0
  fi
  if is_executable "$bin_dir/python3"; then
    printf '%s\n' "$bin_dir/python3"
    return 0
  fi

  local shebang
  shebang="$(head -n 1 "$real_bin" 2>/dev/null || true)"
  if [ "${shebang#\#!}" != "$shebang" ]; then
    local target="${shebang#\#!}"
    local interpreter="${target%% *}"
    local base
    base="$(basename "$interpreter")"
    if [ "$base" = "env" ]; then
      local env_resolved
      env_resolved="$(resolve_env_shebang_utility "$target" || true)"
      if [ -n "$env_resolved" ]; then
        printf '%s\n' "$env_resolved"
        return 0
      fi
      local shell_inferred
      shell_inferred="$(infer_python_from_shell_launcher "$real_bin" || true)"
      if [ -n "$shell_inferred" ]; then
        printf '%s\n' "$shell_inferred"
        return 0
      fi
    elif looks_like_python_executable "$interpreter" && is_executable "$interpreter"; then
      printf '%s\n' "$interpreter"
      return 0
    elif [ "$base" = "sh" ] || [ "$base" = "bash" ] || [ "$base" = "dash" ] || [ "$base" = "zsh" ]; then
      local shell_inferred
      shell_inferred="$(infer_python_from_shell_launcher "$real_bin" || true)"
      if [ -n "$shell_inferred" ]; then
        printf '%s\n' "$shell_inferred"
        return 0
      fi
    fi
  fi
  return 1
}

scan_filesystem_for_hermes() {
  local found=""
  while IFS= read -r candidate; do
    if is_executable "$candidate"; then
      found="$candidate"
      break
    fi
  done < <(
    timeout 4s find / \
      \( -path /proc -o -path /sys -o -path /dev -o -path /run -o -path /tmp -o -path /var/tmp -o -path /mnt -o -path /media \) -prune \
      -o -type f -name hermes -perm /111 -print 2>/dev/null || true
  )
  [ -n "$found" ] && printf '%s\n' "$found"
}

detect_hermes_bin() {
  local hermes_bin
  hermes_bin="$(find_hermes_in_path)"
  if [ -n "$hermes_bin" ]; then
    printf '%s\n' "$hermes_bin"
    return 0
  fi
  scan_filesystem_for_hermes || true
}

detect_hermes_home() {
  local explicit_home="${1:-}"
  local hermes_bin="${2:-}"
  if [ -n "$explicit_home" ]; then
    printf '%s\n' "$explicit_home"
    return 0
  fi
  if [ -n "${HERMES_HOME:-}" ]; then
    printf '%s\n' "$HERMES_HOME"
    return 0
  fi
  if [ -n "$hermes_bin" ]; then
    local config_path
    config_path="$($hermes_bin config path 2>/dev/null | tail -n 1 || true)"
    case "$config_path" in
      */profiles/*/config.yaml)
        printf '%s\n' "${config_path%%/profiles/*}"
        return 0
        ;;
      */config.yaml)
        printf '%s\n' "${config_path%/config.yaml}"
        return 0
        ;;
    esac
  fi
  printf '%s\n' "$HOME/.hermes"
}

select_python_bin() {
  local hermes_home="${1:-}"
  local hermes_bin="${2:-}"
  local explicit_python="${3:-}"
  if [ -n "$explicit_python" ]; then
    printf '%s\n' "$explicit_python"
    return 0
  fi
  if [ -n "${HOII_HERMES_PYTHON:-}" ]; then
    printf '%s\n' "$HOII_HERMES_PYTHON"
    return 0
  fi
  if [ -n "${HERMES_PYTHON:-}" ]; then
    printf '%s\n' "$HERMES_PYTHON"
    return 0
  fi
  if [ -n "$hermes_bin" ]; then
    local inferred
    inferred="$(infer_python_from_hermes_bin "$hermes_bin" || true)"
    if [ -n "$inferred" ]; then
      printf '%s\n' "$inferred"
      return 0
    fi
  fi
  if is_executable "$hermes_home/hermes-agent/venv/bin/python"; then
    printf '%s\n' "$hermes_home/hermes-agent/venv/bin/python"
    return 0
  fi
  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "$PYTHON"
    return 0
  fi
  printf '%s\n' "python3"
}

needs_bootstrap_deps() {
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import yaml, rich, prompt_toolkit
PY
}

should_bootstrap_deps() {
  if [ "${HOII_SKIP_BOOTSTRAP_DEPS:-}" = "1" ]; then
    return 1
  fi
  local has_yes=0
  local has_dry_run=0
  local arg
  for arg in "$@"; do
    case "$arg" in
      --yes|-y) has_yes=1 ;;
      --dry-run) has_dry_run=1 ;;
    esac
  done
  # Keep noninteractive dry-run read-only; real noninteractive installs still
  # bootstrap dependencies so config rendering does not fail late.
  if [ "$has_yes" = "1" ] && [ "$has_dry_run" = "1" ]; then
    return 1
  fi
  return 0
}

install_bootstrap_deps() {
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
  echo "Error: failed to install TUI dependencies." >&2
  echo "Python: ${PYTHON_BIN}" >&2
  echo "Manual command:" >&2
  echo "  ${PYTHON_BIN} -m pip install 'PyYAML>=6' 'rich>=13' 'prompt_toolkit>=3'" >&2
  echo "Then rerun this installer." >&2
  return 1
}

tty_available() {
  [ -e /dev/tty ] || return 1
  { : < /dev/tty; } 2>/dev/null
}

run_installer() {
  if should_bootstrap_deps "$@"; then
    install_bootstrap_deps
  fi
  local installer_args=(
    --source-dir "$SRC_DIR"
    --hermes-home "$DETECTED_HERMES_HOME"
    --hermes-python "$PYTHON_BIN"
    --hermes-bin "$DETECTED_HERMES_BIN"
    "$@"
  )
  if [ -t 0 ]; then
    exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" "${installer_args[@]}"
  elif tty_available; then
    exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" "${installer_args[@]}" < /dev/tty
  else
    exec "$PYTHON_BIN" "$SRC_DIR/scripts/install.py" "${installer_args[@]}"
  fi
}

CLI_HERMES_HOME="$(arg_value --hermes-home "$@" || true)"
CLI_HERMES_PYTHON="$(arg_value --hermes-python "$@" || true)"
CLI_HERMES_BIN="$(arg_value --hermes-bin "$@" || true)"
if [ -n "$CLI_HERMES_BIN" ]; then
  DETECTED_HERMES_BIN="$CLI_HERMES_BIN"
else
  DETECTED_HERMES_BIN="$(detect_hermes_bin)"
fi
DETECTED_HERMES_HOME="$(detect_hermes_home "$CLI_HERMES_HOME" "$DETECTED_HERMES_BIN")"
PYTHON_BIN="$(select_python_bin "$DETECTED_HERMES_HOME" "$DETECTED_HERMES_BIN" "$CLI_HERMES_PYTHON")"

if [ -n "$DETECTED_HERMES_HOME" ]; then
  export HERMES_HOME="$DETECTED_HERMES_HOME"
fi
if [ -n "$DETECTED_HERMES_BIN" ] && [ -z "${HERMES_BIN:-}" ]; then
  export HERMES_BIN="$DETECTED_HERMES_BIN"
fi
if [ -z "${HOII_HERMES_PYTHON:-}" ] && [ -n "$PYTHON_BIN" ] && [ "$PYTHON_BIN" != "python3" ]; then
  export HOII_HERMES_PYTHON="$PYTHON_BIN"
fi

if [ -n "$SOURCE_DIR" ]; then
  SRC_DIR="$SOURCE_DIR"
elif [ -n "$SELF_DIR" ] && [ -d "$SELF_DIR/openai-compatible-image" ] && [ -f "$SELF_DIR/scripts/install.py" ]; then
  SRC_DIR="$SELF_DIR"
else
  TMP_DIR="$(mktemp -d)"
  ARCHIVE_URL="${HOII_ARCHIVE_URL:-https://github.com/${REPO_SLUG}/archive/${REF}.tar.gz}"
  echo "Downloading ${REPO_SLUG}@${REF}..." >&2
  curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_DIR" --strip-components=1
  SRC_DIR="$TMP_DIR"
fi

run_installer "$@"
