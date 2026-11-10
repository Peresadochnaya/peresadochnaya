import os
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .data_status import load_status
from .schemas import DataStatus, ErrorResponse, GeocodedPlace, GeocodeResponse, Leg, Place, RoadRouteRequest, RoadRouteResponse, Route, RouteRequest, RoutesResponse, Suggestion, SuggestResponse, TransitLine, ValidationErrorResponse, TicketProfileInfo
from .yandex import ProviderError, YandexAddresses
from .yandex_router import YandexRouter, dummy_road_route


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
    coordinates = [[point.lon, point.lat] for point in points]
    distance = 0.0
    for left, right in zip(points, points[1:]):
        lat1, lat2 = math.radians(left.lat), math.radians(right.lat)
        dlat, dlon = lat2 - lat1, math.radians(right.lon - left.lon)
        h = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
        distance += 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))
    return Route(id="dummy:route-1", name="Демонстрационный маршрут", distance_meters=round(distance, 2),
                 geometry={"type": "LineString", "coordinates": coordinates},
                 start_at=request.departure_at, end_at=start,
                 duration_seconds=1500, transfers=1, legs=legs)


def create_app(data_root: Path | None = None, planner_mode: str | None = None,
               address_provider: YandexAddresses | None = None,
               road_provider: YandexRouter | None = None) -> FastAPI:
    root = data_root or Path(os.getenv("DATA_ROOT", "/data"))
    mode = planner_mode or os.getenv("PLANNER_MODE", "unavailable")
    if mode not in ("dummy", "unavailable"):
        raise ValueError("PLANNER_MODE must be dummy or unavailable until planner integration is implemented")
    app = FastAPI(title="Пересадочная API", version="0.1.0")
    addresses = address_provider or YandexAddresses()
    roads = road_provider or YandexRouter()

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request, exc):
        return error(exc.status, exc.code, exc.message)

    address_errors = {status: {"model": ErrorResponse} for status in (502, 503, 504)}
    address_errors[422] = {"model": ErrorResponse | ValidationErrorResponse}
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

    @app.get("/api/v1/suggest", response_model=SuggestResponse,
             description="Address suggestions biased towards Saint Petersburg and Leningrad Oblast. Resolve the selected URI through the geocoder to obtain coordinates. Yandex is contacted only with dummy=false.",
             responses=address_errors)
    def suggest(q: str = Query(min_length=2, max_length=256, pattern=r"\S"),
                limit: int = Query(default=5, ge=1, le=10),
                dummy: bool = Query(default=True, description="Synthetic response without Yandex calls; explicitly pass false to enable provider access")):
        if dummy:
            return SuggestResponse(mode="dummy", items=[Suggestion(title=f"Демо: {q.strip()}",
                subtitle="Синтетическая подсказка, не результат поиска", uri="dummy:suggestion:1")],
                warnings=["Synthetic suggestion; Yandex was not called. Dummy URIs must not be sent to Yandex"])
        return addresses.suggest(q.strip(), limit)

    @app.get("/api/v1/geocode", response_model=GeocodeResponse, responses=address_errors,
             description="Resolve exactly one of q (address) or uri (selected suggestion). At most one result; no match yields items=[].")
    def geocode(q: str | None = Query(default=None, min_length=2, max_length=256, pattern=r"\S"),
                uri: str | None = Query(default=None, min_length=1, max_length=4096, pattern=r"\S"),
                dummy: bool = Query(default=True)):
        if (q is None) == (uri is None):
            return error(422, "invalid_geocode_query", "Provide exactly one of q or uri")
        if dummy:
            return GeocodeResponse(mode="dummy", items=[GeocodedPlace(lat=59.956433, lon=30.310138,
                label="Демонстрационная точка, не результат поиска")],
                warnings=["Synthetic fixed coordinates; Yandex was not called"])
        if uri is not None and not uri.startswith("ymapsbm1://"):
            return error(422, "invalid_geocode_uri", "Provide a Yandex suggestion URI; dummy URIs cannot be resolved in real mode")
        return addresses.geocode(q=q.strip() if q else None, uri=uri)

    @app.get("/api/v1/reverse", response_model=GeocodeResponse, responses=address_errors,
             description="Resolve coordinates to an address. Result coordinates belong to the found object; keep the original clicked point for routing.")
    def reverse(lat: float = Query(ge=-90, le=90, allow_inf_nan=False),
                lon: float = Query(ge=-180, le=180, allow_inf_nan=False),
                dummy: bool = Query(default=True)):
        if dummy:
            return GeocodeResponse(mode="dummy", items=[GeocodedPlace(lat=lat, lon=lon,
                label=f"Демо: точка {lon:.5f}, {lat:.5f}")],
                warnings=["Synthetic address label; Yandex was not called"])
        return addresses.geocode(lat=lat, lon=lon)

    @app.post("/api/v1/road-route", response_model=RoadRouteResponse, responses=address_errors,
              description="Driving estimate for exactly two ordered points, independently of OTP. One traffic mode per call, at most one Yandex request with dummy=false. Not a public transport route. No automatic traffic comparison.")
    def road_route(request: RoadRouteRequest,
                   dummy: bool = Query(default=True, description="Synthetic driving estimate; explicitly pass false to spend one Router API request")):
        if dummy:
            return dummy_road_route(request)
        return roads.estimate(request)

    @app.post("/api/v1/routes", response_model=RoutesResponse,
              responses={422: {"model": ErrorResponse | ValidationErrorResponse}, 503: {"model": ErrorResponse}})
    def routes(request: RouteRequest,
               dummy: bool = Query(default=True, description="Synthetic response without Yandex calls; explicitly pass false to enable real planning and provider access")):
        if dummy:
            if request.departure_at.replace(tzinfo=None) > datetime.max - timedelta(seconds=1500):
                return error(422, "invalid_departure_time", "Departure time leaves no representable space for route arrival")
            return RoutesResponse(mode="dummy", passenger=request.passenger, routes=[dummy_route(request)], gtfs_version=None,
                                  graph_version=None, calculated_at=datetime.now(timezone.utc),
                                  warnings=["Synthetic demonstration route; no planner, GTFS, traffic or real transit data was used",
                                            "Ticket profile is recorded only; fare, eligibility and pass coverage are not calculated"])
        data = load_status(root)
        if data.manifest and data.manifest.get("active_service_ids") == 0:
            service_date = str(data.manifest.get("service_date", "")).replace("-", "")
            if request.departure_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d") == service_date:
                return error(422, "invalid_service_date", "GTFS has no active services on the requested date")
        return error(503, "planner_unavailable", "A route planner is not configured")

    return app


app = create_app()
