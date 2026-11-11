"""Synthetic vehicle markers tied to the shared GTFS demo, never fake live data."""
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from .schemas import ErrorResponse
from .vehicles_schemas import VehiclePosition, VehiclesResponse


DEMO_TIMESTAMP = datetime(2026, 11, 11, 9, tzinfo=timezone.utc)
FEED_INTERVAL_SECONDS = 30
ALLOWED_MODES = {"bus", "tram", "trolleybus", "metro", "rail", "unknown"}


def _bbox(value: str) -> tuple[float, float, float, float]:
    try:
        parts = tuple(float(part) for part in value.split(","))
        if len(parts) != 4 or not all(math.isfinite(part) for part in parts):
            raise ValueError
        west, south, east, north = parts
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError
        return parts
    except ValueError:
        raise HTTPException(422, [{"type": "value_error", "loc": ["query", "bbox"],
            "msg": "bbox must be minLon,minLat,maxLon,maxLat within WGS84 bounds, with min < max",
            "input": value}]) from None


def _modes(value: str | None) -> set[str] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part or part not in ALLOWED_MODES for part in parts):
        raise HTTPException(422, [{"type": "value_error", "loc": ["query", "modes"],
            "msg": "modes must contain supported vehicle types separated by commas", "input": value}])
    return set(parts)


def demo_positions(root: Path) -> list[VehiclePosition]:
    # Imported lazily so the isolated router can be mounted independently.
    # The GTFS agent owns this fixture and its stable real transit identifiers.
    from .gtfs_store import GtfsStore

    items = []
    with GtfsStore(root).open(dummy=True) as snapshot:
        for index, trip_id in enumerate(sorted(snapshot.trips)):
            trip, _ = snapshot.trip(trip_id)
            if not trip.stop_times:
                continue
            stop_time = trip.stop_times[min(index, len(trip.stop_times) - 1)]
            stop = stop_time.stop
            route = snapshot.routes[trip.route_id]
            mode = route.mode if route.mode in ALLOWED_MODES else "unknown"
            items.append(VehiclePosition(
                vehicle_id=f"dummy:vehicle:{trip.trip_id}",
                route_id=trip.route_id, trip_id=trip.trip_id,
                lat=stop.lat, lon=stop.lon, mode=mode,
                timestamp=DEMO_TIMESTAMP,
                label=f"Демо: {route.short_name or route.long_name}",
            ))
    return items


def create_vehicles_router(root: Path, *, clock: Callable[[], datetime] | None = None) -> APIRouter:
    router = APIRouter()
    now = clock or (lambda: datetime.now(timezone.utc))

    @router.get("/api/v1/vehicles", response_model=VehiclesResponse,
                description="Synthetic positions tied to the GTFS demo. GTFS-RT is not connected: dummy=false returns 503.",
                responses={503: {"model": ErrorResponse}})
    def vehicles(
        bbox: str = Query(pattern=r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$"),
        modes: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=2000),
        dummy: bool = Query(default=True),
    ):
        west, south, east, north = _bbox(bbox)
        accepted_modes = _modes(modes)
        if not dummy:
            return JSONResponse(status_code=503, content={"error": {
                "code": "realtime_unavailable", "message": "GTFS-RT vehicle positions are not configured",
            }})
        items = [item for item in demo_positions(root)
                 if west <= item.lon <= east and south <= item.lat <= north
                 and (accepted_modes is None or item.mode in accepted_modes)]
        warnings = ["Synthetic vehicle positions on recorded GTFS stops; no GTFS-RT observations were used",
                    "Demo positions are frozen at 2026-11-11T09:00:00Z, not current vehicle locations"]
        if len(items) > limit:
            warnings.append("vehicles_truncated")
        return VehiclesResponse(mode="dummy", items=items[:limit], updated_at=DEMO_TIMESTAMP,
                                stale=(now() - DEMO_TIMESTAMP).total_seconds() > 2 * FEED_INTERVAL_SECONDS,
                                warnings=warnings)

    return router
