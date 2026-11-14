#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_linux
initialize_config
load_config

for executable in uv node npm python3 ffmpeg pkg-config cc git systemctl; do
    require_command "$executable"
done
pkg-config --exists libusb-1.0 || fail 'Missing libusb development files; install libusb-1.0-0-dev.'
python3 - "$(node --version)" "$MORPH_STUDIO_NODE_MIN" "$(npm --version)" "$MORPH_STUDIO_NPM_MIN" <<'PY'
import re
import sys

def version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", text.strip())
    if not match:
        raise SystemExit(f"Cannot parse version: {text!r}")
    return tuple(map(int, match.groups()))

for name, actual, minimum in (("Node", *sys.argv[1:3]), ("npm", *sys.argv[3:5])):
    if version(actual) < version(minimum):
        raise SystemExit(f"{name} {minimum} or newer required; found {actual}")
PY

umask 077
mkdir -p -- "$STORAGE_DIR" "$UV_CACHE_DIR" "$HF_HOME" "$HF_LEROBOT_HOME" "$XDG_CACHE_HOME"
cd -- "$MORPH_STUDIO_ROOT/application/backend"
uv sync --locked --python "$MORPH_STUDIO_PYTHON" --extra "$MORPH_STUDIO_ACCELERATOR" --no-default-groups
cd -- "$MORPH_STUDIO_ROOT/application/ui"
npm ci
npm run build
"$MORPH_LINUX_TOOLS_DIR/service.sh" install
printf '%s\n' 'Setup complete. Start with tools/linux/service.sh start, then tools/linux/verify.sh.'
