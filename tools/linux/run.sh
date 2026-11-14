#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_linux
load_config
require_backend
umask 077
cd -- "$MORPH_STUDIO_ROOT/application/backend"
exec .venv/bin/physicalai-studio serve --host "$HOST" --port "$PORT"
