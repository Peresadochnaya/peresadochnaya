import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .data_status import load_status
from .schemas import DataStatus, ErrorResponse, Leg, Place, Route, RouteRequest, RoutesResponse, TransitLine, ValidationErrorResponse, TicketProfileInfo


TICKET_PROFILES = [
    TicketProfileInfo(id=key, label_ru=label) for key, label in [
        ("full_fare", "Без проездного, полный тариф"),
        ("podorozhnik_wallet", "Единый электронный билет на «Подорожнике»"),
        ("monthly_unified", "Месячный единый"),
        ("monthly_surface", "Месячный наземный (трамвай, троллейбус, автобус)"),
        ("monthly_bus", "Месячный автобусный"),
        ("monthly_metro", "Месячный метрополитен"),
        ("student_unified", "Студенческий единый"),
        ("student_surface", "Студенческий наземный"),
        ("student_metro", "Студенческий метрополитен"),
        ("school_unified", "Ученический единый"),
        ("school_bus", "Ученический автобусный"),
        ("concession_unified", "Льготный единый"),
    ]
]


def error(status: int, code: str, message: str):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def dummy_route(request: RouteRequest) -> Route:
    """Synthetic fixture for frontend development, not a geographic itinerary."""
    start = request.departure_at
    a, b = request.points
    points = [Place(lat=a.lat + (b.lat-a.lat)*fraction, lon=a.lon+(b.lon-a.lon)*fraction,
                    name=name, stop_id=f"dummy:{i}" if i in (1, 2, 3, 4) else None)
              for i, (fraction, name) in enumerate([(0, "Origin"), (.1, "Dummy boarding stop"),
                 (.45, "Dummy transfer stop A"), (.5, "Dummy transfer stop B"),
                 (.9, "Dummy alighting stop"), (1, "Destination")])]
    legs = []
    for i, (mode, seconds) in enumerate([("walk", 180), ("bus", 600), ("walk", 120), ("tram", 480), ("walk", 120)]):
        end = start + timedelta(seconds=seconds)
        legs.append(Leg(mode=mode, origin=points[i], destination=points[i+1], start_at=start,
                        end_at=end, duration_seconds=seconds, original_duration_seconds=seconds,
                        duration_source="dummy", line=TransitLine(short_name="DEMO", route_id=f"dummy:{mode}") if mode != "walk" else None,
                        geometry={"type": "LineString", "coordinates": [[points[i].lon, points[i].lat], [points[i+1].lon, points[i+1].lat]]}))
        start = end
    return Route(id="dummy:route-1", start_at=request.departure_at, end_at=start,
                 duration_seconds=1500, transfers=1, legs=legs)


def create_app(data_root: Path | None = None, planner_mode: str | None = None) -> FastAPI:
    root = data_root or Path(os.getenv("DATA_ROOT", "/data"))
    mode = planner_mode or os.getenv("PLANNER_MODE", "unavailable")
    if mode not in ("dummy", "unavailable"):
        raise ValueError("PLANNER_MODE must be dummy or unavailable until planner integration is implemented")
    app = FastAPI(title="Пересадочная API", version="0.1.0")
    origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        # API readiness is distinct from planner/data availability, exposed below.
        return {"status": "ready", "planner_mode": mode}

    @app.get("/api/v1/data/status", response_model=DataStatus)
    def data_status():
        return load_status(root)

    @app.get("/api/v1/ticket-profiles", response_model=list[TicketProfileInfo])
    def ticket_profiles():
        return TICKET_PROFILES

    @app.post("/api/v1/routes", response_model=RoutesResponse,
              responses={422: {"model": ErrorResponse | ValidationErrorResponse}, 503: {"model": ErrorResponse}})
    def routes(request: RouteRequest):
        data = load_status(root)
        if mode == "dummy":
            if request.departure_at.replace(tzinfo=None) > datetime.max - timedelta(seconds=1500):
                return error(422, "invalid_departure_time", "Departure time leaves no representable space for route arrival")
            return RoutesResponse(mode="dummy", passenger=request.passenger, routes=[dummy_route(request)], gtfs_version=None,
                                  graph_version=None, calculated_at=datetime.now(timezone.utc),
                                  warnings=["Synthetic demonstration route; no planner, GTFS, traffic or real transit data was used",
                                            "Ticket profile is recorded only; fare, eligibility and pass coverage are not calculated"])
        if data.manifest and data.manifest.get("active_service_ids") == 0:
            service_date = str(data.manifest.get("service_date", "")).replace("-", "")
            if request.departure_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d") == service_date:
                return error(422, "invalid_service_date", "GTFS has no active services on the requested date")
        return error(503, "planner_unavailable", "A route planner is not configured")

    return app


app = create_app()
