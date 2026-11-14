#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_linux
load_config
require_command systemctl
unit="$MORPH_STUDIO_SERVICE_NAME.service"

install_service() {
    require_backend
    require_command systemd-analyze
    local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    local unit_file="$unit_dir/$unit"
    if [[ -e "$unit_file" ]] && ! grep -q '^# Managed by Morph tools/linux$' "$unit_file"; then
        fail "Existing unit was not created by these tools: $unit_file"
    fi
    mkdir -p -- "$unit_dir"
    local temporary_dir temporary
    temporary_dir="$(mktemp -d "$unit_dir/.morph-service.XXXXXX")"
    temporary="$temporary_dir/$unit"
    if ! python3 "$MORPH_LINUX_TOOLS_DIR/write_unit.py" "$MORPH_STUDIO_ROOT" "$MORPH_STUDIO_ENV_FILE" > "$temporary"; then
        rm -- "$temporary"
        rmdir -- "$temporary_dir"
        fail 'Could not generate the service unit; the existing unit was preserved.'
    fi
    if ! systemd-analyze --user verify "$temporary"; then
        rm -- "$temporary"
        rmdir -- "$temporary_dir"
        fail 'Generated service failed systemd validation; the existing unit was preserved.'
    fi
    mv -- "$temporary" "$unit_file"
    rmdir -- "$temporary_dir"
    systemctl --user daemon-reload
    systemctl --user enable "$unit"
}

case "${1:-}" in
    install) install_service ;;
    start|stop|restart) systemctl --user "$1" "$unit" ;;
    status) systemctl --user status "$unit" --no-pager ;;
    logs) journalctl --user-unit "$unit" --no-pager -n 80 ;;
    *) fail 'Usage: tools/linux/service.sh {install|start|stop|restart|status|logs}' ;;
esac
