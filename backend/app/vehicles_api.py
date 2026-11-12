"""Synthetic vehicle markers tied to the shared GTFS demo, never fake live data."""
import math
import os
import httpx
from . import realtime
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from .schemas import ErrorResponse
from .vehicles_schemas import VehiclePosition, VehiclesResponse


DEMO_TIMESTAMP = datetime(2026, 11, 11, 9, tzinfo=timezone.utc)
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


def demo_positions(root: Path, at: datetime | None = None) -> list[VehiclePosition]:
    from .gtfs_store import GtfsStore, seconds

    at = at or datetime.now(timezone.utc)
    local = at.astimezone(ZoneInfo("Europe/Moscow"))
    phase = (local.hour * 3600 + local.minute * 60 + local.second) % 600
    items = []
    with GtfsStore(root).open(dummy=True) as snapshot:
        for trip_id in sorted(snapshot.trips):
            trip, _ = snapshot.trip(trip_id)
            if not trip.stop_times:
                continue
            base = seconds(trip.stop_times[0].departure_time)
            duration = seconds(trip.stop_times[-1].arrival_time) - base
            for elapsed in range(phase, duration + 1, 600):
                current = trip.stop_times[0]
                for call in trip.stop_times:
                    if seconds(call.arrival_time) - base <= elapsed:
                        current = call
                route = snapshot.routes[trip.route_id]
                # Each marker is a synthetic departure on the same grid exposed
                # by /departures, held at its most recently passed GTFS stop.
                started = int(at.timestamp()) - elapsed
                items.append(VehiclePosition(
                    vehicle_id=f"dummy:vehicle:{trip.trip_id}:{started}",
                    route_id=trip.route_id, trip_id=trip.trip_id,
                    lat=current.stop.lat, lon=current.stop.lon,
                    mode=route.mode if route.mode in ALLOWED_MODES else "unknown",
                    timestamp=at, label=f"Демо: {route.short_name or route.long_name}",
                ))
    return items


def create_vehicles_router(root: Path, *, clock: Callable[[], datetime] | None = None) -> APIRouter:
    router = APIRouter()
    now = clock or (lambda: datetime.now(timezone.utc))

    @router.get("/api/v1/vehicles", response_model=VehiclesResponse,
                description="Synthetic positions tied to the GTFS demo. dummy=false reads observed positions from the separate GTFS-RT service; bbox and modes filter before limit. heading is observed bearing in degrees clockwise from north, or null. Stale data is explicitly marked; 503 before the first successful generation.",
                responses={503: {"model": ErrorResponse}})
    def vehicles(
        bbox: str = Query(pattern=r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$"),
        modes: str | None = Query(default=None),
        limit: int = Query(default=10000, ge=1, le=20000),
        dummy: bool = Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники"),
    ):
        west, south, east, north = _bbox(bbox)
        accepted_modes = _modes(modes)
        if not dummy and os.getenv('REALTIME_URL'):
            try:
                result = realtime.request('/api/v1/vehicles', {'bbox': bbox,
                    'limit': limit, **({'modes': ','.join(sorted(accepted_modes))} if accepted_modes else {})})
                return VehiclesResponse.model_validate(result.json())
            except (httpx.HTTPError, ValueError):
                return JSONResponse(status_code=503, content={"error": {
                    "code": "realtime_unavailable", "message": "GTFS-RT vehicle positions are not available"}})
        if not dummy:
            return JSONResponse(status_code=503, content={"error": {
                "code": "realtime_unavailable", "message": "GTFS-RT vehicle positions are not configured",
            }})
        at = now()
        items = [item for item in demo_positions(root, at)
                 if west <= item.lon <= east and south <= item.lat <= north
                 and (accepted_modes is None or item.mode in accepted_modes)]
        warnings = ["Synthetic positions on the shared GTFS demo and its all-day 10-minute timetable; no GTFS-RT observations were used",
                    "Timestamps describe demo generation, not actual vehicle observations"]
        if len(items) > limit:
            warnings.append("vehicles_truncated")
        return VehiclesResponse(mode="dummy", items=items[:limit], updated_at=at, stale=False,
                                warnings=warnings)

    return router
