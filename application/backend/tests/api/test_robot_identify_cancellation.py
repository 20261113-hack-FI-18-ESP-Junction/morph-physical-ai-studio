import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from physicalai.robot import device_id_from_serial_port

from api import robot_catalog
from exceptions import RobotDeviceAlreadyOwnedError
from workers.robot_worker import claim_devices


@pytest.mark.parametrize("plugin_fails", [False, True])
async def test_cancelled_identify_keeps_claim_until_plugin_finishes(monkeypatch, tmp_path, plugin_fails):
    port = str(tmp_path / "serial")
    device_ids = (device_id_from_serial_port(port),)
    monkeypatch.setattr(robot_catalog, "find_so101_port", AsyncMock(return_value=port))
    started, finish, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def identify(*args):
        started.set()
        await finish.wait()
        finished.set()
        if plugin_fails:
            raise RuntimeError("test plugin failure after cancellation")

    definition = SimpleNamespace(type="SO101_Follower", probe=SimpleNamespace(identify=identify))
    request = asyncio.create_task(robot_catalog.identify_robot(definition, object(), {"connection_string": port}))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for _ in range(2):
            request.cancel()
            await asyncio.sleep(0)
            assert not request.done()
            assert not finished.is_set()
            with pytest.raises(RobotDeviceAlreadyOwnedError), claim_devices("calibration", device_ids):
                pytest.fail("Cancelled Identify released its claim while motor activity continued")
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=2)

    assert finished.is_set()
    with claim_devices("next-worker", device_ids):
        pass
