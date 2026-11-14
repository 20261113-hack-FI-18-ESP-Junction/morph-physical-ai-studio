import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Body, Depends
from fastapi.responses import FileResponse
from physicalai.robot import device_id_from_serial_port
from physicalai_so101_plugin.studio_catalog import find_so101_port
from physicalai_studio_plugin import RobotCatalogDefinition
from pydantic import BaseModel, Field

from api.dependencies import RobotCatalogServiceDep, RobotConnectionManagerDep
from exceptions import ResourceNotFoundError, ResourceType, RobotIdentifyError
from robots.catalog.assets import resolve_robot_relative_asset_path, resolve_robot_urdf_path
from schemas import SerialPortInfo
from workers.robot_worker import claim_devices


class RobotZeroCalibrationResponse(BaseModel):
    instructions: str = Field(..., description="Description of the zero pose shown in the calibration wizard")
    zero_tolerance_deg: float = Field(..., description="Largest joint reading, in degrees, that counts as zero")


class RobotCatalogDefinitionResponse(BaseModel):
    type: str = Field(..., description="Stable backend robot type identifier")
    display_name: str = Field(..., description="Human-readable robot type label")
    role: Literal["follower", "leader"] = Field(..., description="Default robot role")
    urdf_path: str = Field(description="URDF URL used by the UI model loader")
    package_map: dict[str, str] = Field(default_factory=dict, description="URDF package name to URL prefix map")
    joint_map: dict[str, list[str]] = Field(
        description="Observation joint name to URDF joint(s) mapping",
    )
    zero_calibration: RobotZeroCalibrationResponse | None = Field(
        default=None,
        description="Zero-pose calibration offered when adding this robot type, if any",
    )


def _to_response(definition: RobotCatalogDefinition) -> RobotCatalogDefinitionResponse:
    catalog_root = f"/api/robots/catalog/{definition.type}"

    package_map = {}
    joint_map = {}
    if definition.asset is not None:
        package_map = dict.fromkeys(definition.asset.packages, f"{catalog_root}")
        joint_map = definition.asset.joint_map

    return RobotCatalogDefinitionResponse(
        type=definition.type,
        display_name=definition.display_name,
        role=definition.role,
        urdf_path=f"{catalog_root}/urdf",
        package_map=package_map,
        joint_map=joint_map,
        zero_calibration=None
        if definition.zero_calibration is None
        else RobotZeroCalibrationResponse(
            instructions=definition.zero_calibration.instructions,
            zero_tolerance_deg=definition.zero_calibration.zero_tolerance_deg,
        ),
    )


def _get_robot_definition(
    robot_type: str,
    catalog_service: RobotCatalogServiceDep,
) -> RobotCatalogDefinition:
    return catalog_service.get_definition(robot_type)


def _validate_probe_payload(
    payload: Annotated[dict[str, Any], Body(...)],
    definition: Annotated[RobotCatalogDefinition, Depends(_get_robot_definition)],
) -> BaseModel | dict[str, Any]:
    if definition.robot_payload is None:
        return payload
    return definition.robot_payload.model_validate(payload)


RobotDefinitionDep = Annotated[RobotCatalogDefinition, Depends(_get_robot_definition)]
ProbePayloadDep = Annotated[BaseModel | dict[str, Any], Depends(_validate_probe_payload)]


router = APIRouter(prefix="/api/robots/catalog", tags=["Robot Catalog"])
SO101_TYPES = frozenset({"SO101_Follower", "SO101_Leader"})


def _is_so101_overload_protection_error(error: RuntimeError) -> bool:
    """Return whether a plugin error represents the SO101 identify recovery action."""
    return "stopped responding during motion" in str(error) and "overload protection" in str(error)


@asynccontextmanager
async def _claim_identify_device(
    robot_type: str,
    robot_manager: RobotConnectionManagerDep,
    payload: BaseModel | dict[str, Any],
) -> AsyncIterator[None]:
    """Keep SO101 identification from opening a device held by another worker."""
    if robot_type not in SO101_TYPES:
        yield
        return

    values = payload.model_dump() if isinstance(payload, BaseModel) else payload
    port = await find_so101_port(
        robot_manager,
        SerialPortInfo(
            connection_string=values.get("connection_string") or None,
            serial_number=values.get("serial_number") or None,
        ),
    )
    if port is None:
        raise ValueError("Could not resolve the SO101 serial device for identification")
    with claim_devices(f"studio-identify-{uuid4().hex}", (device_id_from_serial_port(port),)):
        yield


async def _complete_identify(operation: Coroutine[Any, Any, None]) -> None:
    """Keep ownership until thread-backed motor activity ends, even on cancellation."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break  # Retrieve the plugin error below, after its task has completed.
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    task.result()


@router.get("")
async def list_robot_catalog(catalog_service: RobotCatalogServiceDep) -> list[RobotCatalogDefinitionResponse]:
    """List robot catalog definitions exposed to the UI."""
    return [_to_response(definition) for definition in catalog_service.list_entries()]


@router.get("/{robot_type}/discover")  # noqa: FAST003 - consumed by RobotDefinitionDep via _get_robot_definition
async def discover_robots(
    definition: RobotDefinitionDep,
    robot_manager: RobotConnectionManagerDep,
) -> list[SerialPortInfo]:
    """Discover connected devices for a robot type."""
    if definition.probe is None:
        return []
    return await definition.probe.discover(robot_manager)


@router.post("/{robot_type}/identify")  # noqa: FAST003 - consumed by RobotDefinitionDep via _get_robot_definition
async def identify_robot(
    definition: RobotDefinitionDep,
    robot_manager: RobotConnectionManagerDep,
    payload: ProbePayloadDep,
    joint: str | None = None,
) -> None:
    """Visually identify a robot by moving a joint or gripper."""
    if definition.probe is None:
        raise ResourceNotFoundError(
            resource_type=ResourceType.ROBOT,
            resource_id=definition.type,
            message=f"Robot type {definition.type} does not support identification.",
        )
    try:
        async with _claim_identify_device(definition.type, robot_manager, payload):
            operation = definition.probe.identify(payload, robot_manager, joint)
            if definition.type in SO101_TYPES:
                await _complete_identify(operation)
            else:
                await operation
    except RuntimeError as error:
        if _is_so101_overload_protection_error(error):
            raise RobotIdentifyError(str(error)) from error
        raise


@router.post("/{robot_type}/is-online")  # noqa: FAST003 - consumed by RobotDefinitionDep via _get_robot_definition
async def check_robot_online(
    definition: RobotDefinitionDep,
    payload: ProbePayloadDep,
) -> bool:
    """Check if a robot is currently online/reachable."""
    if definition.probe is None:
        return False
    return await definition.probe.is_online(payload)


@router.get("/{robot_type}/urdf")  # noqa: FAST003 - consumed by RobotDefinitionDep via _get_robot_definition
async def get_robot_catalog_urdf(
    definition: RobotDefinitionDep,
) -> FileResponse:
    """Return the URDF file for a catalog robot type."""
    resolved_path = resolve_robot_urdf_path(definition)
    return FileResponse(resolved_path)


@router.get("/{robot_type}/schema")  # noqa: FAST003 - consumed by RobotDefinitionDep via _get_robot_definition
async def get_robot_catalog_schema(
    definition: RobotDefinitionDep,
) -> dict[str, Any]:
    """Return the Pydantic JSON Schema for a catalog robot payload."""
    payload = definition.robot_payload
    if payload is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Robot type {definition.type} has no payload schema")
    return payload.model_json_schema()


@router.get("/{robot_type}/{asset_path:path}")  # noqa: FAST003 - consumed by RobotDefinitionDep via _get_robot_definition
async def get_robot_catalog_asset(
    definition: RobotDefinitionDep,
    asset_path: Path,
) -> FileResponse:
    """Return an asset referenced by a relative URDF URI."""
    resolved_path = resolve_robot_relative_asset_path(definition, asset_path=asset_path)
    return FileResponse(resolved_path)
