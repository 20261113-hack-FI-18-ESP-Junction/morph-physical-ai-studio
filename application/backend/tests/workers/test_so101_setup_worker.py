import errno
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from physicalai.robot import device_id_from_serial_port

from schemas import SerialPortInfo
from workers.robot_worker import claim_devices
from workers.robots import so101_setup_worker
from workers.transport.worker_transport import WorkerTransport


@pytest.fixture
def setup_worker(monkeypatch, tmp_path):
    port = str(tmp_path / "serial")
    monkeypatch.setattr(so101_setup_worker, "find_so101_port", AsyncMock(return_value=port))
    bus = MagicMock()
    bus.port_handler.ser.fileno.return_value = 123
    monkeypatch.setattr(so101_setup_worker, "FeetechMotorsBus", MagicMock(return_value=bus))
    transport = MagicMock(spec=WorkerTransport)
    worker = so101_setup_worker.SO101SetupWorker(
        transport=transport,
        robot_type="SO101_Follower",
        serial_port=SerialPortInfo(serial_number="test-arm"),
        robot_manager=MagicMock(),
    )
    return worker, bus, transport


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl/termios are POSIX modules")
async def test_linux_exclusive_claim_immediately_follows_open(monkeypatch, setup_worker):
    import fcntl
    import termios

    worker, bus, _ = setup_worker
    events = []
    bus.connect.side_effect = lambda **kwargs: events.append(("connect", kwargs))
    monkeypatch.setattr(so101_setup_worker, "sys", SimpleNamespace(platform="linux"))
    ioctl = MagicMock(side_effect=lambda fd, request: events.append(("ioctl", fd, request)))
    monkeypatch.setattr(fcntl, "ioctl", ioctl)

    try:
        await worker._connect_bus()
        assert events == [("connect", {"handshake": False}), ("ioctl", 123, termios.TIOCEXCL)]
        bus.read.assert_not_called()
        bus.write.assert_not_called()
        bus.ping.assert_not_called()
    finally:
        await worker._cleanup()

    bus.disconnect.assert_called_once_with()
    assert worker.bus is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux terminal exclusivity")
def test_linux_exclusive_claim_blocks_an_ordinary_second_open(setup_worker):
    import fcntl
    import os
    import pty
    import termios

    worker, bus, _ = setup_worker
    master, slave = pty.openpty()  # A private virtual terminal, never a robot device.
    bus.port_handler.ser.fileno.return_value = slave
    worker.bus = bus
    try:
        worker._open_bus_exclusively()
        try:
            second = os.open(os.ttyname(slave), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as error:
            assert error.errno == errno.EBUSY
        else:
            os.close(second)
            pytest.fail("Another process can open the exclusive terminal (check CAP_SYS_ADMIN)")
    finally:
        fcntl.ioctl(slave, termios.TIOCNXCL)
        os.close(slave)
        os.close(master)


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl/termios are POSIX modules")
@pytest.mark.parametrize("error_number", [errno.EBUSY, errno.ENOTTY, errno.EIO])
async def test_exclusive_failure_closes_without_motor_traffic(monkeypatch, setup_worker, error_number):
    import fcntl

    worker, bus, transport = setup_worker
    monkeypatch.setattr(so101_setup_worker, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(fcntl, "ioctl", MagicMock(side_effect=OSError(error_number, "exclusive claim failed")))
    worker._run_voltage_check = AsyncMock()
    worker._run_motor_probe = AsyncMock()

    await worker.run()

    bus.connect.assert_called_once_with(handshake=False)
    worker._run_voltage_check.assert_not_awaited()
    worker._run_motor_probe.assert_not_awaited()
    bus.disconnect.assert_not_called()
    bus.disable_torque.assert_not_called()
    bus.write.assert_not_called()
    bus.read.assert_not_called()
    bus.ping.assert_not_called()
    bus.port_handler.closePort.assert_called_once_with()
    assert worker.bus is None
    assert any(call.args[0]["event"] == "error" for call in transport.send_json.await_args_list)
    transport.close.assert_awaited_once_with()
    with claim_devices("next-worker", (device_id_from_serial_port(worker.port),)):
        pass


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_other_platforms_keep_existing_connect_and_cleanup(monkeypatch, setup_worker, platform):
    worker, bus, _ = setup_worker
    monkeypatch.setattr(so101_setup_worker, "sys", SimpleNamespace(platform=platform))

    try:
        await worker._connect_bus()
    finally:
        await worker._cleanup()

    bus.connect.assert_called_once_with(handshake=False)
    bus.port_handler.ser.fileno.assert_not_called()
    bus.disconnect.assert_called_once_with()


async def test_failed_open_closes_without_torque_cleanup(setup_worker):
    worker, bus, _ = setup_worker
    bus.connect.side_effect = ConnectionError("test port open failed")
    worker._run_voltage_check = AsyncMock()
    worker._run_motor_probe = AsyncMock()

    await worker.run()

    worker._run_voltage_check.assert_not_awaited()
    worker._run_motor_probe.assert_not_awaited()
    bus.disconnect.assert_not_called()
    bus.port_handler.closePort.assert_called_once_with()


async def test_cleanup_failure_still_closes_port_and_releases_claim(monkeypatch, setup_worker):
    worker, bus, _ = setup_worker
    monkeypatch.setattr(so101_setup_worker, "sys", SimpleNamespace(platform="darwin"))
    await worker._connect_bus()
    bus.disconnect.side_effect = OSError("test disconnect failed")

    await worker._cleanup()

    bus.port_handler.closePort.assert_called_once_with()
    assert worker.bus is None
    with claim_devices("next-worker", (device_id_from_serial_port(worker.port),)):
        pass
