#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_linux
load_config
require_backend
exec "$MORPH_STUDIO_ROOT/application/backend/.venv/bin/python" "$MORPH_LINUX_TOOLS_DIR/verify.py"
