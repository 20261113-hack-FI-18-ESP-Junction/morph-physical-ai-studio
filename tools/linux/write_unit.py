"""Generate the user service without shell-interpolating paths."""

import sys
from pathlib import Path


def quoted(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Service paths must not contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_text(root: Path, config: Path) -> str:
    # This scalar path setting retains quotes literally, unlike ExecStart.
    backend = str(root / "application/backend").replace("%", "%%")
    return f"""# Managed by Morph tools/linux
[Unit]
Description=Morph Physical AI Studio workstation
After=network.target

[Service]
Type=exec
WorkingDirectory={backend}
Environment={quoted('MORPH_STUDIO_ENV_FILE=' + str(config))}
ExecStart={quoted(str(root / 'tools/linux/run.sh'))}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


if __name__ == "__main__":
    print(unit_text(Path(sys.argv[1]), Path(sys.argv[2])), end="")
