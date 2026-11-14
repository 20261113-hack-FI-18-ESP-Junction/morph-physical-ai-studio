#!/usr/bin/env bash
# Shared configuration; callers source this file once.
set -euo pipefail

MORPH_LINUX_TOOLS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MORPH_STUDIO_ROOT="$(cd -- "$MORPH_LINUX_TOOLS_DIR/../.." && pwd -P)"
MORPH_STUDIO_ENV_FILE="${MORPH_STUDIO_ENV_FILE:-$MORPH_STUDIO_ROOT/.runtime/linux.env}"

fail() { printf '%s\n' "$*" >&2; exit 1; }

require_linux() {
    [[ "$(uname -s)" == Linux ]] || fail 'Run this command on the Linux robot workstation.'
}

initialize_config() {
    if [[ ! -e "$MORPH_STUDIO_ENV_FILE" ]]; then
        mkdir -p -- "$(dirname -- "$MORPH_STUDIO_ENV_FILE")"
        (umask 077; cp -- "$MORPH_LINUX_TOOLS_DIR/studio.env.example" "$MORPH_STUDIO_ENV_FILE")
    fi
}

load_config() {
    [[ "$MORPH_STUDIO_ENV_FILE" == /* ]] || fail 'MORPH_STUDIO_ENV_FILE must be an absolute path.'
    [[ -f "$MORPH_STUDIO_ENV_FILE" ]] || fail 'Missing local config. Run tools/linux/setup.sh first.'
    set -a
    source "$MORPH_STUDIO_ENV_FILE"
    set +a
    case "$HOST" in 127.0.0.1|localhost|::1) ;; *) fail 'HOST must be a loopback address.' ;; esac
    [[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT > 0 && PORT < 65536)) || fail 'PORT must be 1–65535.'
    [[ -z "$PUBLIC_API_BASE_URL" ]] || fail 'PUBLIC_API_BASE_URL must be empty for same-origin serving.'
    [[ "$MORPH_STUDIO_ACCELERATOR" == xpu || "$MORPH_STUDIO_ACCELERATOR" == cpu ]] || fail 'Select xpu or cpu.'
    [[ "$MORPH_STUDIO_SERVICE_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail 'Invalid service name.'
    [[ "$MORPH_STUDIO_READY_TIMEOUT" =~ ^[0-9]+$ ]] || fail 'Readiness timeout must be a positive integer.'
    ((MORPH_STUDIO_READY_TIMEOUT > 0)) || fail 'Readiness timeout must be a positive integer.'
    [[ "$MORPH_STUDIO_MIN_CAMERAS" =~ ^[0-9]+$ ]] || fail 'Minimum camera count must be a nonnegative integer.'
    for variable in STORAGE_DIR SETTINGS_FILE UV_CACHE_DIR HF_HOME HF_LEROBOT_HOME XDG_CACHE_HOME; do
        [[ "${!variable}" == "$MORPH_STUDIO_ROOT/.runtime/"* ]] || fail "$variable must stay inside .runtime/."
        [[ "/${!variable}/" != *'/../'* ]] || fail "$variable must not contain parent traversal."
    done
}

require_command() {
    command -v "$1" >/dev/null || fail "Missing prerequisite: $1. Install it before running setup."
}

require_backend() {
    [[ -x "$MORPH_STUDIO_ROOT/application/backend/.venv/bin/physicalai-studio" ]] || fail 'Backend missing. Run setup.sh.'
    [[ -f "$STATIC_FILES_DIR/index.html" ]] || fail 'Built UI missing. Run setup.sh.'
}
