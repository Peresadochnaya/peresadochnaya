"""Public vehicle-position contract; real GTFS-RT is not connected yet."""
from typing import Literal

from pydantic import AwareDatetime, Field

from .schemas import Model, Point


VehicleMode = Literal["bus", "tram", "trolleybus", "metro", "rail", "unknown"]


class VehiclePosition(Point):
    vehicle_id: str
    route_id: str | None = None
    trip_id: str | None = None
    heading: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)
    speed_mps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    mode: VehicleMode
    timestamp: AwareDatetime
    label: str | None = None


class VehiclesResponse(Model):
    mode: Literal["dummy", "realtime"]
    items: list[VehiclePosition]
    updated_at: AwareDatetime
    stale: bool
    warnings: list[str] = Field(default_factory=list)
