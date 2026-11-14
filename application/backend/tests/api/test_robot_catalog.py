import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from physicalai.robot import device_id_from_serial_port
from pydantic import BaseModel

from api import robot_catalog
from exceptions import RobotDeviceAlreadyOwnedError
from main import app
from robots.catalog.assets import resolve_robot_relative_asset_path
from robots.catalog.registry import RobotCatalogRegistry
from workers.robot_worker import claim_devices


def test_get_robot_catalog_urdf_returns_plugin_file() -> None:
    definition = RobotCatalogRegistry().get_definition("SO101_Follower")
    assert definition is not None
    assert definition.asset is not None
    assert definition.asset.root_resolver is not None
    urdf_path = definition.asset.root_resolver() / definition.asset.urdf_relative_path

    client = TestClient(app)

    response = client.get("/api/robots/catalog/SO101_Follower/urdf")

    assert response.status_code == 200
    assert response.content == urdf_path.read_bytes()


def test_list_robot_catalog_returns_definitions_without_internal_fields() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog")

    assert response.status_code == 200
    payload = response.json()

    assert payload
    first = payload[0]
    assert set(first.keys()) == {
        "type",
        "display_name",
        "role",
        "urdf_path",
        "package_map",
        "joint_map",
        "zero_calibration",
    }
    assert "urdf_relative_path" not in first

    so101 = next(definition for definition in payload if definition["type"] == "SO101_Follower")
    assert so101["urdf_path"] == "/api/robots/catalog/SO101_Follower/urdf"
    assert so101["package_map"] == {"SO101": "/api/robots/catalog/SO101_Follower"}
    # SO101 calibrates through its own setup wizard, not the plugin zero-pose calibration.
    assert so101["zero_calibration"] is None


def test_get_robot_catalog_schema_returns_pydantic_schema() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/SO101_Follower/schema")

    assert response.status_code == 200
    schema = response.json()
    assert "serial_number" in schema["properties"]
    assert "connection_string" in schema["properties"]
    assert schema["example"] == {
        "connection_string": "",
        "serial_number": "SO101-2024-001",
        "unit": "normalized",
        "calibration": None,
    }


def test_get_external_robot_catalog_schema_returns_pydantic_schema() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/LeKiwi_Follower/schema")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_check_robot_online_invalid_payload_returns_validation_error() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/robots/catalog/LeKiwi_Follower/is-online",
        json={"connection_string": "/dev/ttyUSB0"},
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_identify_robot_invalid_payload_returns_validation_error() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/robots/catalog/LeKiwi_Follower/identify",
        json={"connection_string": "/dev/ttyUSB0"},
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_discover_unknown_robot_type_returns_not_found() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/UnknownRobot/discover")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_is_online_unknown_robot_type_returns_not_found() -> None:
    client = TestClient(app)

    response = client.post("/api/robots/catalog/UnknownRobot/is-online", json={})

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_identify_unknown_robot_type_returns_not_found() -> None:
    client = TestClient(app)

    response = client.post("/api/robots/catalog/UnknownRobot/identify", json={})

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_get_robot_catalog_urdf_unknown_robot_type_returns_not_found() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/UnknownRobot/urdf")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_get_robot_catalog_schema_unknown_robot_type_returns_not_found() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/UnknownRobot/schema")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_get_robot_catalog_asset_unknown_robot_type_returns_not_found() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/UnknownRobot/packages/SO101/meshes/base.stl")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error_code"] == "Robot_not_found"


def test_get_robot_catalog_asset_returns_plugin_file() -> None:
    definition = RobotCatalogRegistry().get_definition("SO101_Follower")
    assert definition is not None
    assert definition.asset is not None
    asset_path = resolve_robot_relative_asset_path(definition, Path("assets/base_so101_v2.stl"))

    client = TestClient(app)

    response = client.get("/api/robots/catalog/SO101_Follower/assets/base_so101_v2.stl")

    assert response.status_code == 200
    assert response.content == asset_path.read_bytes()


def test_get_robot_catalog_asset_rejects_traversal() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/SO101_Follower/%2E%2E/secret.txt")

    assert response.status_code == 403


def test_get_robot_catalog_asset_returns_404_for_missing_file() -> None:
    client = TestClient(app)

    response = client.get("/api/robots/catalog/SO101_Follower/missing.stl")

    assert response.status_code == 404


class _IdentifyPayload(BaseModel):
    serial_number: str


@pytest.mark.parametrize("robot_type", ["SO101_Follower", "SO101_Leader"])
@pytest.mark.parametrize("as_model", [False, True])
async def test_identify_refuses_a_claimed_so101_before_calling_plugin(monkeypatch, tmp_path, robot_type, as_model):
    port = str(tmp_path / "serial")
    resolver = AsyncMock(return_value=port)
    monkeypatch.setattr(robot_catalog, "find_so101_port", resolver)
    identify = AsyncMock()
    definition = SimpleNamespace(type=robot_type, probe=SimpleNamespace(identify=identify))
    payload = _IdentifyPayload(serial_number="test-arm") if as_model else {"serial_number": "test-arm"}

    with (
        claim_devices("calibration", (device_id_from_serial_port(port),)),
        pytest.raises(RobotDeviceAlreadyOwnedError),
    ):
        await robot_catalog.identify_robot(definition, object(), payload)

    identify.assert_not_awaited()
    assert resolver.await_args.args[1].serial_number == "test-arm"


@pytest.mark.parametrize("plugin_fails", [False, True])
async def test_identify_holds_claim_until_plugin_finishes_then_releases(monkeypatch, tmp_path, plugin_fails):
    port = str(tmp_path / "serial")
    device_ids = (device_id_from_serial_port(port),)
    monkeypatch.setattr(robot_catalog, "find_so101_port", AsyncMock(return_value=port))

    async def identify(*args):
        await asyncio.sleep(0)
        with pytest.raises(RobotDeviceAlreadyOwnedError), claim_devices("other-worker", device_ids):
            pytest.fail("Identify released the device while the plugin was still running")
        if plugin_fails:
            raise RuntimeError("test plugin failure")

    definition = SimpleNamespace(type="SO101_Follower", probe=SimpleNamespace(identify=identify))
    if plugin_fails:
        with pytest.raises(RuntimeError, match="test plugin failure"):
            await robot_catalog.identify_robot(definition, object(), {"connection_string": port})
    else:
        await robot_catalog.identify_robot(definition, object(), {"connection_string": port})

    with claim_devices("next-worker", device_ids):
        pass


async def test_identify_does_not_change_other_robot_types(monkeypatch):
    resolver = AsyncMock()
    monkeypatch.setattr(robot_catalog, "find_so101_port", resolver)
    identify = AsyncMock()
    definition = SimpleNamespace(type="Other_Follower", probe=SimpleNamespace(identify=identify))
    manager, payload = object(), {"connection_string": "other-connection"}

    await robot_catalog.identify_robot(definition, manager, payload, "gripper")

    resolver.assert_not_awaited()
    identify.assert_awaited_once_with(payload, manager, "gripper")


async def test_identify_unresolved_so101_never_calls_plugin(monkeypatch):
    monkeypatch.setattr(robot_catalog, "find_so101_port", AsyncMock(return_value=None))
    identify = AsyncMock()
    definition = SimpleNamespace(type="SO101_Follower", probe=SimpleNamespace(identify=identify))

    with pytest.raises(ValueError, match="Could not resolve"):
        await robot_catalog.identify_robot(definition, object(), {"serial_number": "missing"})

    identify.assert_not_awaited()
