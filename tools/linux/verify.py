"""Check the local Studio service and compute runtime without commanding robots."""

import json
import os
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    base_url: str
    timeout: int
    minimum_cameras: int
    accelerator: str
    service_name: str
    root: Path

    @classmethod
    def from_env(cls):
        host = os.environ["HOST"]
        host = f"[{host}]" if ":" in host else host
        return cls(
            f"http://{host}:{int(os.environ['PORT'])}",
            int(os.environ["MORPH_STUDIO_READY_TIMEOUT"]),
            int(os.environ["MORPH_STUDIO_MIN_CAMERAS"]),
            os.environ["MORPH_STUDIO_ACCELERATOR"],
            os.environ["MORPH_STUDIO_SERVICE_NAME"],
            Path(__file__).resolve().parents[2],
        )


def fetch(config: Config, path: str, *, as_json=True):
    with urllib.request.urlopen(config.base_url + path, timeout=15) as response:
        body = response.read()
        return json.loads(body) if as_json else body.decode("utf-8")


def wait_for_health(config: Config):
    deadline = time.monotonic() + config.timeout
    while True:
        try:
            health = fetch(config, "/api/health")
            if health.get("status") == "healthy":
                return health
            raise RuntimeError(f"Unexpected health response: {health}")
        except (OSError, ValueError, RuntimeError) as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Studio did not become healthy: {error}") from error
            time.sleep(1)


def owns_listener(pid: int, port: int, proc_root=Path("/proc")):
    """Match listening TCP sockets to descriptors owned by the service process."""
    process = proc_root / str(pid)
    sockets = set()
    for descriptor in (process / "fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue  # A descriptor can close while the process is running.
        if target.startswith("socket:[") and target.endswith("]"):
            sockets.add(target[8:-1])
    for protocol in ("tcp", "tcp6"):
        table = process / "net" / protocol
        if not table.exists():
            continue
        for line in table.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 10 and fields[3] == "0A":
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
                if local_port == port and fields[9] in sockets:
                    return True
    return False


def check_service(config: Config, *, require_listener=True, proc_root=Path("/proc")):
    unit = f"{config.service_name}.service"
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=ActiveState,MainPID"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    pid = int(properties.get("MainPID", "0"))
    if properties.get("ActiveState") != "active" or pid <= 0:
        raise RuntimeError(f"{unit} is not running; inspect service.sh status and logs, then start it")
    backend = (config.root / "application/backend").resolve()
    if (proc_root / str(pid) / "cwd").resolve(strict=True) != backend:
        raise RuntimeError(f"{unit} is running from another checkout; reinstall and restart this service")
    port = urllib.parse.urlsplit(config.base_url).port
    if require_listener and not owns_listener(pid, port, proc_root):
        raise RuntimeError(f"{unit} PID {pid} does not own port {port}; another process may be serving Studio")
    return {"unit": unit, "pid": pid, "backend": str(backend)}


def check_linger():
    try:
        result = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return result.stdout.strip() if result.stdout.strip() in {"yes", "no"} else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def check_source(config: Config, info):
    result = subprocess.run(
        ["git", "-C", str(config.root), "rev-parse", "--show-toplevel", "HEAD"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    source_root, revision = result.stdout.strip().splitlines()
    if Path(source_root).resolve() != config.root.resolve():
        raise RuntimeError("Studio must be its own Git checkout to verify the served source revision")
    with (config.root / "application/backend/pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    if info.get("git_sha") != revision or info.get("studio_version") != version:
        raise RuntimeError(
            f"Served Studio provenance differs from this checkout: expected {version} at {revision}, "
            f"received {info.get('studio_version')} at {info.get('git_sha')}. "
            "Run setup.sh and restart this service after updating the checkout."
        )


def check_devices(config: Config, inference, training):
    if not any(device.get("backend") == "torch" and device.get("type") == config.accelerator
               for device in inference):
        raise RuntimeError(f"The served backend does not expose Torch {config.accelerator} inference")
    if training.get("mode") != "local" or training.get("remote_available") is not True:
        raise RuntimeError("The served backend must expose available local training devices")
    if not any(device.get("type") == config.accelerator for device in training.get("devices", [])):
        raise RuntimeError(f"The served backend does not expose local {config.accelerator} training")


def check_api(config: Config):
    service = check_service(config, require_listener=False)
    health = wait_for_health(config)
    if check_service(config)["pid"] != service["pid"]:
        raise RuntimeError("Studio restarted during verification; check service logs and retry")
    info = fetch(config, "/api/system/info")
    check_source(config, info)
    html = fetch(config, "/", as_json=False)
    if "<html" not in html.lower() or "Physical AI Studio" not in html:
        raise RuntimeError("The root URL did not serve the Studio UI")
    catalog = fetch(config, "/api/robots/catalog")
    types = {item["type"] for item in catalog}
    if not {"SO101_Leader", "SO101_Follower"}.issubset(types):
        raise RuntimeError("The SO-101 leader/follower plugins are missing")
    cameras = fetch(config, "/api/hardware/cameras?all=true")
    if len(cameras) < config.minimum_cameras:
        raise RuntimeError(f"Expected at least {config.minimum_cameras} cameras; discovered {len(cameras)}")
    inference = fetch(config, "/api/system/devices/inference")
    training = fetch(config, "/api/system/devices/training")
    check_devices(config, inference, training)
    if check_service(config)["pid"] != service["pid"]:
        raise RuntimeError("Studio restarted during verification; check service logs and retry")
    return {
        "health": health, "service": service, "studio": info,
        "robot_types": sorted(types), "camera_names": [camera["name"] for camera in cameras],
        "inference_devices": inference, "training_devices": training,
    }


def check_compute(config: Config):
    import openvino
    import torch

    available = torch.xpu.is_available()
    if config.accelerator == "xpu" and not available:
        raise RuntimeError("XPU was selected, but PyTorch cannot access an Intel GPU")
    device = "xpu" if config.accelerator == "xpu" else "cpu"
    value = torch.arange(8, dtype=torch.float32, device=device).square().sum().item()
    if value != 140.0:
        raise RuntimeError(f"Compute check returned {value}; expected 140")
    return {"torch": torch.__version__, "device": device, "openvino_devices": openvino.Core().available_devices}


def main():
    try:
        config = Config.from_env()
        result = {"ok": True, "hostname": socket.gethostname(), "url": config.base_url}
        result["api"] = check_api(config)
        result["compute"] = check_compute(config)
        result["user_linger"] = check_linger()
        result["scope"] = "Service, plugins, camera enumeration, and compute; no robot motion or camera-frame capture."
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
