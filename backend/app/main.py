import os
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api_docs import API_DESCRIPTION, OPERATION_DESCRIPTIONS
from .data_status import load_status
from . import demo_addresses
from .dgis import DgisAddresses, DgisRouter
from .gtfs_api import create_gtfs_router
from .vehicles_api import create_vehicles_router
from .recorded_dummy import recorded_geocode, recorded_reverse, recorded_road_route, recorded_suggest
from .schemas import DataStatus, ErrorResponse, GeoProvider, GeocodedPlace, GeocodeResponse, Leg, Place, RoadRouteRequest, RoadRouteResponse, Route, RouteRequest, RouteProvenance, RoutesResponse, RoutesResponseSources, Suggestion, SuggestResponse, TransitLine, ValidationErrorResponse, TicketProfileInfo
from .yandex import ProviderError, YandexAddresses
from .yandex_router import YandexRouter, dummy_road_route


TICKET_PROFILES = [
    TicketProfileInfo(id="podorozhnik_wallet", label_ru="Только «Подорожник» — электронный кошелёк",
        description_ru="В 2026 году пересадочный тариф городского наземного транспорта: 65 ₽ за первую, 14 ₽ за вторую, 0 ₽ за последующие посадки в пределах 60 минут от первой валидации. Это не скользящее окно; метро прекращает наземную пересадочную цепочку. Электрички и областные маршруты не считаются включёнными.",
        transfer_window_seconds=3600),
    TicketProfileInfo(id="unlimited_surface", label_ru="Безлимит только наземный",
        covered_modes=["bus", "tram", "trolleybus"],
        description_ru="Профиль уже действующего проездного на городской наземный транспорт. Метро, электрички и областные маршруты не считаются покрытыми."),
    TicketProfileInfo(id="unlimited_metro", label_ru="Безлимит только метро", covered_modes=["metro"],
        description_ru="Профиль уже действующего проездного на метро. Наземные поездки оплачиваются отдельно; их способ оплаты и тариф требуют отдельного расчёта."),
    TicketProfileInfo(id="unlimited_all", label_ru="Полный городской безлимит", covered_modes=["bus", "tram", "trolleybus", "metro"],
        description_ru="Профиль уже действующего проездного на городской наземный транспорт и метро. Не обещает покрытие электричек, областных или иных специальных маршрутов; условия конкретного билета отдельно проверяются."),
    TicketProfileInfo(id="bank_card", label_ru="Всегда банковской картой, без скидок",
        description_ru="Каждая посадка оплачивается по полному банковскому тарифу соответствующего перевозчика; скидки ЕКП/Мир, пересадочные скидки и кешбэк не применяются."),
]


def error(status: int, code: str, message: str):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def point_distance(left: Place, right: Place) -> float:
    lat1, lat2 = math.radians(left.lat), math.radians(right.lat)
    dlat, dlon = lat2 - lat1, math.radians(right.lon - left.lon)
    h = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


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
                        duration_source="dummy", distance_meters=round(point_distance(points[i], points[i+1]), 2),
                        line=TransitLine(short_name="DEMO", route_id=f"dummy:{mode}") if mode != "walk" else None,
                        geometry={"type": "LineString", "coordinates": [[points[i].lon, points[i].lat], [points[i+1].lon, points[i+1].lat]]}))
        start = end
    coordinates = [[point.lon, point.lat] for point in points]
    distance = round(sum(leg.distance_meters for leg in legs), 2)
    return Route(id="dummy:route-1", name="Демонстрационный маршрут", distance_meters=distance,
                 geometry={"type": "LineString", "coordinates": coordinates},
                 start_at=request.departure_at, end_at=start,
                 duration_seconds=1500, transfers=1, legs=legs,
                 provenance=RouteProvenance(candidate_providers=[], traffic_providers=[]))


def create_app(data_root: Path | None = None, planner_mode: str | None = None,
               address_provider: YandexAddresses | None = None,
               road_provider: YandexRouter | None = None,
               dgis_address_provider: DgisAddresses | None = None,
               dgis_road_provider: DgisRouter | None = None) -> FastAPI:
    root = data_root or Path(os.getenv("DATA_ROOT", "/data"))
    mode = planner_mode or os.getenv("PLANNER_MODE", "unavailable")
    if mode not in ("dummy", "unavailable"):
        raise ValueError("PLANNER_MODE must be dummy or unavailable until planner integration is implemented")
    app = FastAPI(title="Пересадочная API", version="0.2.0", description=API_DESCRIPTION)
    app.include_router(create_gtfs_router(root))
    app.include_router(create_vehicles_router(root))
    addresses = address_provider or YandexAddresses()
    roads = road_provider or YandexRouter()
    dgis_addresses = dgis_address_provider or DgisAddresses()
    dgis_roads = dgis_road_provider or DgisRouter()

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
                provider: GeoProvider = Query(default="yandex"),
                dummy: bool = Query(default=True, description="Recorded or synthetic demo response without Yandex calls; explicitly pass false to enable provider access")):
        if dummy:
            recorded = recorded_suggest(q, limit) if provider == "yandex" else None
            if recorded is not None:
                return recorded
            return demo_addresses.suggestions(provider, q, limit)
        return (addresses if provider == "yandex" else dgis_addresses).suggest(q.strip(), limit)

    @app.get("/api/v1/geocode", response_model=GeocodeResponse, responses=address_errors,
             description="Resolve exactly one of q (address) or uri (selected suggestion). At most one result; no match yields items=[].")
    def geocode(q: str | None = Query(default=None, min_length=2, max_length=256, pattern=r"\S"),
                uri: str | None = Query(default=None, min_length=1, max_length=4096, pattern=r"\S"),
                provider: GeoProvider = Query(default="yandex"),
                dummy: bool = Query(default=True)):
        if (q is None) == (uri is None):
            return error(422, "invalid_geocode_query", "Provide exactly one of q or uri")
        if dummy:
            if uri is not None and uri.startswith("dummy:") and uri.startswith(f"dummy:{'2gis' if provider == 'yandex' else 'yandex'}:"):
                return error(422, "invalid_geocode_uri", "Suggestion provider does not match geocode provider")
            recorded = recorded_geocode(q, uri) if provider == "yandex" else None
            if recorded is not None:
                return recorded
            return demo_addresses.geocode(provider, q, uri)
        prefix = "ymapsbm1://" if provider == "yandex" else "dgis:"
        if uri is not None and not uri.startswith(prefix):
            return error(422, "invalid_geocode_uri", "Provide a suggestion identifier from the selected provider; dummy URIs cannot be resolved in real mode")
        return (addresses if provider == "yandex" else dgis_addresses).geocode(q=q.strip() if q else None, uri=uri)

    @app.get("/api/v1/reverse", response_model=GeocodeResponse, responses=address_errors,
             description="Resolve coordinates to an address. Result coordinates belong to the found object; keep the original clicked point for routing.")
    def reverse(lat: float = Query(ge=-90, le=90, allow_inf_nan=False),
                lon: float = Query(ge=-180, le=180, allow_inf_nan=False),
                provider: GeoProvider = Query(default="yandex"),
                dummy: bool = Query(default=True)):
        if dummy:
            recorded = recorded_reverse(lat, lon) if provider == "yandex" else None
            if recorded is not None:
                return recorded
            return demo_addresses.reverse(provider, lat, lon)
        return (addresses if provider == "yandex" else dgis_addresses).geocode(lat=lat, lon=lon)

    @app.post("/api/v1/road-route", response_model=RoadRouteResponse, responses=address_errors,
              description="Driving estimate for exactly two ordered points, independently of OTP. One traffic mode per call, at most one Yandex request with dummy=false. Not a public transport route. No automatic traffic comparison.")
    def road_route(request: RoadRouteRequest,
                   provider: GeoProvider = Query(default="yandex"),
                   dummy: bool = Query(default=True, description="Recorded or synthetic driving sample; explicitly pass false to spend one Router API request")):
        if dummy:
            recorded = recorded_road_route(request) if provider == "yandex" else None
            response = recorded if recorded is not None else dummy_road_route(request)
            response.provider = provider
            return response
        return (roads if provider == "yandex" else dgis_roads).estimate(request)

    @app.post("/api/v1/routes", response_model=RoutesResponse,
              responses={422: {"model": ErrorResponse | ValidationErrorResponse},
                         **{status: {"model": ErrorResponse} for status in (502, 503, 504)}})
    def routes(request: RouteRequest,
               dummy: bool = Query(default=True, description="Synthetic response without Yandex calls; explicitly pass false to enable real planning and provider access")):
        if dummy:
            if request.departure_at.replace(tzinfo=None) > datetime.max - timedelta(seconds=1500):
                return error(422, "invalid_departure_time", "Departure time leaves no representable space for route arrival")
            return RoutesResponse(mode="dummy", sources=RoutesResponseSources(
                candidate_providers=[], traffic_providers=[], realtime="dummy"), passenger=request.passenger, routes=[dummy_route(request)], gtfs_version=None,
                                  graph_version=None, calculated_at=datetime.now(timezone.utc),
                                  warnings=["Synthetic demonstration route; no planner, GTFS, traffic or real transit data was used",
                                            "Ticket profile is recorded only; fare, eligibility and pass coverage are not calculated"])
        data = load_status(root)
        if data.manifest and data.manifest.get("active_service_ids") == 0:
            service_date = str(data.manifest.get("service_date", "")).replace("-", "")
            if request.departure_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d") == service_date:
                return error(422, "invalid_service_date", "GTFS has no active services on the requested date")
        return error(503, "planner_unavailable", "A route planner is not configured")

    # Apply documentation to the generated schema, including lazily included routers.
    generated_openapi = app.openapi

    def documented_openapi():
        document = generated_openapi()
        for path, description in OPERATION_DESCRIPTIONS.items():
            for operation in document["paths"].get(path, {}).values():
                operation["description"] = description
        return document

    app.openapi = documented_openapi
    return app


app = create_app()
