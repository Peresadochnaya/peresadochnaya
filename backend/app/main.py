import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import urlencode

from .api_docs import API_DESCRIPTION, OPERATION_DESCRIPTIONS
from .data_status import load_status, read_object
from .otp import OtpPlanner
from .traffic_engine import TrafficEngine
from .traffic_planner import ObservationCollector
from .traffic_graph import graph_for_active
from . import demo_addresses
from .demo_routes import compose as dummy_route
from .dgis import DgisAddresses, DgisRouter
from .gtfs_api import create_gtfs_router
from .vehicles_api import create_vehicles_router
from .realtime import create_realtime_router
from .schemas import DataStatus, ErrorResponse, GeoProvider, GeocodedPlace, GeocodeResponse, RoadRouteRequest, RoadRouteResponse, RouteRequest, RoutesResponse, RoutesResponseSources, Suggestion, SuggestResponse, ValidationErrorResponse, TicketProfileInfo
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



def _active_transfer_slack(root):
    """Read the graph's actual router defaults, never infer an extra margin."""
    try:
        config=read_object(root / 'otp' / 'current' / 'router-config.json')
        if not config or 'routingDefaults' not in config:
            return None
        defaults=config['routingDefaults']
        total=0.
        for name in ('transferSlack','boardSlack','alightSlack'):
            value=defaults.get(name,0)
            if isinstance(value,(int,float)):
                total+=max(0,value)
            elif isinstance(value,str):
                match=re.fullmatch(r'PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?',value)
                if match:
                    total+=sum(float(n or 0)*unit for n,unit in zip(match.groups(),(3600,60,1)))
        return total
    except (OSError,ValueError,TypeError):
        return None


def create_app(data_root: Path | None = None, planner_mode: str | None = None,
               address_provider: YandexAddresses | None = None,
               road_provider: YandexRouter | None = None,
               dgis_address_provider: DgisAddresses | None = None,
               dgis_road_provider: DgisRouter | None = None,
               transit_planner: OtpPlanner | None = None,
               traffic_graph_loader=None) -> FastAPI:
    root = data_root or Path(os.getenv("DATA_ROOT", "/data"))
    mode = planner_mode or os.getenv("PLANNER_MODE", "unavailable")
    if mode not in ("dummy", "unavailable", "otp"):
        raise ValueError("PLANNER_MODE must be dummy, unavailable or otp")
    @asynccontextmanager
    async def lifespan(application):
        # The compact working topology is warmed once outside user searches.
        try:
            graph = graph_for_active(root)
            if graph is not None:
                graph.warm_topology()
        except Exception:
            application.state.traffic_baseline_warning = 'topology_warmup_unavailable'
        yield

    app = FastAPI(title="Пересадочная API", version="0.2.0", description=API_DESCRIPTION,lifespan=lifespan)
    app.include_router(create_gtfs_router(root))
    app.include_router(create_vehicles_router(root))
    app.include_router(create_realtime_router())
    addresses = address_provider or YandexAddresses()
    roads = road_provider or YandexRouter()
    dgis_addresses = dgis_address_provider or DgisAddresses()
    dgis_roads = dgis_road_provider or DgisRouter()
    planner = transit_planner or OtpPlanner()
    topology_warm_slot=threading.BoundedSemaphore(1)
    def load_traffic_graph(**kwargs):
        graph=graph_for_active(root,**kwargs)
        if graph is not None and not graph.topology_ready and topology_warm_slot.acquire(blocking=False):
            def warm():
                try:
                    graph.warm_topology()
                except Exception:
                    pass
                finally:
                    topology_warm_slot.release()
            threading.Thread(target=warm,name='traffic-topology-warm',daemon=True).start()
        return graph

    traffic_engine = TrafficEngine(planner, roads, dgis_roads,
        graph_loader=traffic_graph_loader or load_traffic_graph,
        collector=ObservationCollector(roads, dgis_roads),
        transfer_slack_loader=lambda: _active_transfer_slack(root))

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
             description="Address suggestions biased towards Saint Petersburg and Leningrad Oblast. Resolve the selected URI through the geocoder to obtain coordinates. The selected provider is contacted only with dummy=false.",
             responses=address_errors)
    def suggest(q: str = Query(min_length=2, max_length=256, pattern=r"\S"),
                limit: int = Query(default=5, ge=1, le=10),
                provider: GeoProvider = Query(default="yandex"),
                dummy: bool = Query(default=True, description="Recorded or synthetic demo response without external API calls; explicitly pass false to enable selected provider access")):
        if dummy:
            return demo_addresses.suggestions(provider, q, limit)
        return (addresses if provider == "yandex" else dgis_addresses).suggest(q.strip(), limit)

    @app.get("/api/v1/geocode", response_model=GeocodeResponse, responses=address_errors,
             description="Resolve exactly one of q (address) or uri (selected suggestion). At most one result; no match yields items=[].")
    def geocode(q: str | None = Query(default=None, min_length=2, max_length=256, pattern=r"\S"),
                uri: str | None = Query(default=None, min_length=1, max_length=4096, pattern=r"\S"),
                provider: GeoProvider = Query(default="yandex"),
                dummy: bool = Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        if (q is None) == (uri is None):
            return error(422, "invalid_geocode_query", "Provide exactly one of q or uri")
        if dummy:
            if uri is not None and uri.startswith("dummy:") and uri.startswith(f"dummy:{'2gis' if provider == 'yandex' else 'yandex'}:"):
                return error(422, "invalid_geocode_uri", "Suggestion provider does not match geocode provider")
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
                dummy: bool = Query(default=True, description="true: согласованные демо-данные без внешних запросов; false: реальные источники")):
        if dummy:
            return demo_addresses.reverse(provider, lat, lon)
        return (addresses if provider == "yandex" else dgis_addresses).geocode(lat=lat, lon=lon)

    @app.post("/api/v1/road-route", response_model=RoadRouteResponse, responses=address_errors,
              description="Driving estimate for exactly two ordered points, independently of OTP. One traffic mode per call, at most one selected provider request with dummy=false. Not a public transport route. No automatic traffic comparison.")
    def road_route(request: RoadRouteRequest,
                   provider: GeoProvider = Query(default="yandex"),
                   dummy: bool = Query(default=True, description="Recorded or synthetic driving sample; explicitly pass false to spend one selected provider request")):
        if dummy:
            response = dummy_road_route(request)
            response.provider = provider
            return response
        return (roads if provider == "yandex" else dgis_roads).estimate(request)

    @app.post("/api/v1/routes", response_model=RoutesResponse,
              responses={422: {"model": ErrorResponse | ValidationErrorResponse},
                         **{status: {"model": ErrorResponse} for status in (502, 503, 504)}})
    def routes(request: RouteRequest,
               dummy: bool = Query(default=True, description="Synthetic response without Yandex calls; explicitly pass false to enable real planning and provider access")):
        if dummy:
            try:
                demo = dummy_route(request)
            except OverflowError:
                return error(422, "invalid_departure_time", "Departure time leaves no representable space for route arrival")
            return RoutesResponse(mode="dummy", sources=RoutesResponseSources(
                candidate_providers=[], traffic_providers=[], realtime="dummy"), passenger=request.passenger, routes=[demo], gtfs_version=None,
                                  graph_version=None, calculated_at=datetime.now(timezone.utc),
                                  warnings=["Shared GTFS demo network with a synthetic all-day 10-minute timetable; no live planner or traffic was used",
                                            "Ticket profile is recorded only; fare, eligibility and pass coverage are not calculated"]
                                  + (["Fare rules are ignored in demo mode"] if request.fare is not None else []))
        data = load_status(root)
        source_calendar_empty = bool(data.manifest and data.manifest.get("active_service_ids") == 0)
        if mode != "otp" and source_calendar_empty:
            service_date = str(data.manifest.get("service_date", "")).replace("-", "")
            if request.departure_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d") == service_date:
                return error(422, "invalid_service_date", "GTFS has no active services on the requested date")
        if mode != "otp":
            return error(503, "planner_unavailable", "A route planner is not configured")
        # The running graph may predate the latest downloaded feed. Report the
        # snapshot used to build it, not a newer ZIP that OTP has not loaded.
        graph_meta = {}
        try:
            graph_meta = read_object(root / "otp" / "current.json") or {}
        except (OSError, ValueError):
            pass
        source_sha = graph_meta.get("gtfs_source_sha256")
        valid_sha = isinstance(source_sha, str) and len(source_sha) == 64 and all(c in "0123456789abcdef" for c in source_sha)
        planned_gtfs = source_sha if valid_sha else data.gtfs_version
        response = (traffic_engine if request.traffic_mode == "on" else planner).plan(request, gtfs_version=planned_gtfs,
                                graph_version=data.graph_version)
        if planned_gtfs != data.gtfs_version:
            response.warnings.append("graph_uses_previous_gtfs: the latest downloaded feed has not been loaded into the running graph")
        if graph_meta.get("gtfs_calendar_fixed"):
            response.warnings.append("graph_calendar_repaired: invalid source calendar end dates were extended in the graph input copy")
        if graph_meta.get("metro_source_sha256") or graph_meta.get("metro_sha256"):
            response.warnings.append("metro_times_estimated: pMetro running times and modelled headways are used, not an exact train timetable")
            for route in response.routes:
                for index, leg in enumerate(route.legs):
                    waiting_for_metro = leg.mode == "wait" and index + 1 < len(route.legs) and route.legs[index + 1].mode == "metro"
                    if leg.mode == "metro" or waiting_for_metro:
                        leg.duration_source = "estimated"
        if graph_meta.get('rail_source_sha256'):
            response.warnings.append(
                f"rail_schedule_limited: suburban schedules cover {graph_meta.get('rail_service_start')} "
                f"through {graph_meta.get('rail_service_end')}; later dates are not extrapolated")
        if source_calendar_empty:
            response.warnings.append(
                "Source GTFS validation reported no active services on its validation date; "
                "date availability is determined by the loaded OTP graph, which may use "
                "a repaired calendar copy and additional feeds. Source validation remains unsuccessful")
        return response

    # Apply documentation to the generated schema, including lazily included routers.
    generated_openapi = app.openapi

    def documented_openapi():
        document = generated_openapi()
        # Python versions use different reason phrases for HTTP 422. Keep the
        # published contract identical in CI, developer environments and Docker.
        for path_item in document["paths"].values():
            for operation in path_item.values():
                validation = operation.get("responses", {}).get("422")
                if validation is not None:
                    validation["description"] = "Unprocessable Content"
        for path, description in OPERATION_DESCRIPTIONS.items():
            for operation in document["paths"].get(path, {}).values():
                operation["description"] = description
        return document

    app.openapi = documented_openapi

    @app.get("/map-api.js", include_in_schema=False)
    def map_api_script():
        key = os.getenv("YANDEX_JS_API_KEY", "")
        if not key:
            return error(503, "map_unavailable", "Map JavaScript API is not configured")
        return RedirectResponse(
            "https://api-maps.yandex.ru/2.1/?" + urlencode({"apikey": key, "lang": "ru_RU"}),
            headers={"Cache-Control": "no-store"})

    # Mount last so API, health and Swagger keep their existing routes.
    frontend = Path(os.getenv("FRONTEND_DIST", "/app/frontend/dist"))
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
