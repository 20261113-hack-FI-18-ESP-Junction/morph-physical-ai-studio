"""Offline checks for local configuration and service generation."""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


write_unit = load_module("write_unit")
verify = load_module("verify")


class LinuxToolTests(unittest.TestCase):
    def config(self, accelerator="xpu", root=TOOLS.parents[1]):
        return verify.Config("http://127.0.0.1:3000", 0, 0, accelerator, "test-studio", root)

    def device(self, kind="xpu", backend=None):
        # Fields match schemas/hardware.py; inference adds backend and device.
        device = {"type": kind, "name": kind.upper(), "memory": None, "index": 0, "busy": None}
        if backend is not None:
            device.update(backend=backend, device=f"{kind}:0" if backend == "torch" else "GPU")
        return device

    def service_files(self, root, *, owned_inode="12345", listener_inode="12345", protocol="tcp"):
        backend = root / "application/backend"
        backend.mkdir(parents=True)
        process = root / "proc/42"
        (process / "fd").mkdir(parents=True)
        (process / "net").mkdir()
        (process / "cwd").symlink_to(backend, target_is_directory=True)
        (process / "fd/7").symlink_to(f"socket:[{owned_inode}]")
        address = "0100007F" if protocol == "tcp" else "00000000000000000000000001000000"
        (process / f"net/{protocol}").write_text(
            "sl local_address rem_address st queues timer retransmit uid timeout inode\n"
            f"0: {address}:0BB8 {address}:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 {listener_inode}\n"
        )
        return root / "proc"

    def check_config(self, extra="", command="load_config"):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "linux.env"
            config.write_text((TOOLS / "studio.env.example").read_text() + extra)
            before = config.read_text()
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; MORPH_STUDIO_ENV_FILE="$2"; ' + command,
                 "test", str(TOOLS / "common.sh"), str(config)],
                capture_output=True, text=True,
            )
            self.assertEqual(config.read_text(), before)
            return result

    def test_all_shell_scripts_parse(self):
        for script in TOOLS.glob("*.sh"):
            result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_config_loads(self):
        result = self.check_config()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_initialization_preserves_existing_config(self):
        result = self.check_config("\nPORT=3123\n", "initialize_config; load_config")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_network_exposure_is_rejected(self):
        result = self.check_config("\nHOST=0.0.0.0\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("loopback", result.stderr)

    def test_external_state_directory_is_rejected(self):
        result = self.check_config("\nSTORAGE_DIR=/tmp/studio-state\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside .runtime", result.stderr)

    def test_static_build_requires_relative_api_urls(self):
        result = self.check_config("\nPUBLIC_API_BASE_URL=http://example.invalid\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("same-origin", result.stderr)

    def test_unit_quotes_paths_and_escapes_systemd_specifiers(self):
        text = write_unit.unit_text(Path("/tmp/studio 100%"), Path("/tmp/local config.env"))
        self.assertIn('ExecStart="/tmp/studio 100%%/tools/linux/run.sh"', text)
        self.assertIn('Environment="MORPH_STUDIO_ENV_FILE=/tmp/local config.env"', text)
        self.assertIn("UMask=0077", text)

    def test_unhealthy_response_is_not_accepted(self):
        config = self.config("cpu")
        with patch.object(verify, "fetch", return_value={"status": "starting"}):
            with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
                verify.wait_for_health(config)

    def test_inactive_service_cannot_pass_via_another_healthy_api(self):
        systemctl = subprocess.CompletedProcess([], 0, "ActiveState=failed\nMainPID=0\n")
        with patch.object(verify.subprocess, "run", return_value=systemctl), patch.object(verify, "fetch") as fetch:
            with self.assertRaisesRegex(RuntimeError, "is not running"):
                verify.check_api(self.config())
            fetch.assert_not_called()

    def test_service_must_own_its_listener(self):
        systemctl = subprocess.CompletedProcess([], 0, "ActiveState=active\nMainPID=42\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            proc = self.service_files(root, listener_inode="99999")
            with patch.object(verify.subprocess, "run", return_value=systemctl):
                with self.assertRaisesRegex(RuntimeError, "does not own port"):
                    verify.check_service(self.config(root=root), proc_root=proc)

    def test_expected_service_accepts_ipv4_and_ipv6_listeners(self):
        systemctl = subprocess.CompletedProcess([], 0, "ActiveState=active\nMainPID=42\n")
        for protocol in ("tcp", "tcp6"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                proc = self.service_files(root, protocol=protocol)
                with patch.object(verify.subprocess, "run", return_value=systemctl):
                    service = verify.check_service(self.config(root=root), proc_root=proc)
                self.assertEqual(service["pid"], 42)

    def test_service_from_another_checkout_is_rejected(self):
        systemctl = subprocess.CompletedProcess([], 0, "ActiveState=active\nMainPID=42\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            proc = self.service_files(root)
            (proc / "42/cwd").unlink()
            (proc / "42/cwd").symlink_to(root, target_is_directory=True)
            with patch.object(verify.subprocess, "run", return_value=systemctl):
                with self.assertRaisesRegex(RuntimeError, "another checkout"):
                    verify.check_service(self.config(root=root), proc_root=proc)

    def test_source_version_and_revision_must_match_but_dirty_work_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            backend = root / "application/backend"
            backend.mkdir(parents=True)
            (backend / "pyproject.toml").write_text('[project]\nversion = "0.3.0"\n')
            revision = "a" * 40
            git = subprocess.CompletedProcess([], 0, f"{root}\n{revision}\n")
            with patch.object(verify.subprocess, "run", return_value=git):
                verify.check_source(self.config(root=root), {
                    "studio_version": "0.3.0", "git_sha": revision, "git_dirty": True,
                })
                for version, sha in (("0.0.0", revision), ("0.3.0", "b" * 40), ("0.3.0", None)):
                    with self.subTest(version=version, sha=sha):
                        with self.assertRaisesRegex(RuntimeError, "provenance differs"):
                            verify.check_source(self.config(root=root), {"studio_version": version, "git_sha": sha})

    def test_served_xpu_requires_torch_inference_and_local_training(self):
        inference = [self.device("cpu", "torch"), self.device("xpu", "torch"), self.device("xpu", "openvino")]
        training = {"mode": "local", "remote_available": True, "devices": [self.device()]}
        verify.check_devices(self.config(), inference, training)
        for devices in ([self.device("cpu", "torch")], [self.device("xpu", "openvino")]):
            with self.subTest(inference=devices):
                with self.assertRaisesRegex(RuntimeError, "Torch xpu inference"):
                    verify.check_devices(self.config(), devices, training)
        for devices in (
            {"mode": "local", "remote_available": True, "devices": [self.device("cpu")]},
            {"mode": "remote", "remote_available": True, "devices": [self.device()]},
            {"mode": "local", "remote_available": False, "devices": [self.device()]},
        ):
            with self.subTest(training=devices):
                with self.assertRaisesRegex(RuntimeError, "local"):
                    verify.check_devices(self.config(), inference, devices)

    def test_explicit_cpu_configuration_does_not_require_xpu(self):
        verify.check_devices(self.config("cpu"), [self.device("cpu", "torch")], {
            "mode": "local", "remote_available": True, "devices": [self.device("cpu")],
        })

    def test_healthy_api_without_served_xpu_fails_full_api_verification(self):
        responses = {
            "/api/health": {"status": "healthy"},
            "/api/system/info": {"studio_version": "0.3.0", "git_sha": "a" * 40},
            "/": "<html><title>Physical AI Studio</title></html>",
            "/api/robots/catalog": [{"type": "SO101_Leader"}, {"type": "SO101_Follower"}],
            "/api/hardware/cameras?all=true": [],
            "/api/system/devices/inference": [self.device("cpu", "torch")],
            "/api/system/devices/training": {
                "mode": "local", "remote_available": True, "devices": [self.device("cpu")],
            },
        }
        with (
            patch.object(verify, "check_service", return_value={"pid": 42}),
            patch.object(verify, "check_source"),
            patch.object(verify, "fetch", side_effect=lambda config, path, **kwargs: responses[path]),
        ):
            with self.assertRaisesRegex(RuntimeError, "Torch xpu inference"):
                verify.check_api(self.config())

    def test_linger_reports_unavailable_without_changing_login_behavior(self):
        with patch.object(verify.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(verify.check_linger(), "unknown")


if __name__ == "__main__":
    unittest.main()
