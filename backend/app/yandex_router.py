"""Road estimates from Router API, independently of OTP and transit algorithms."""
import hashlib
import math
import httpx

from datetime import datetime, timezone

from pydantic import ValidationError

from .schemas import RoadRoute, RoadRouteRequest, RoadRouteResponse
from .yandex import ProviderError, YandexClient


ROUTER_URL = "https://api.routing.yandex.net/v2/route"
DRIVING_WARNING = "Driving estimate only; not a bus duration or a confirmed public transport itinerary"
CACHE_ENTRY_LIFETIME = 900  # seconds (15 minutes)


def driving_params(request: RoadRouteRequest, now: datetime) -> dict:
    params = {"waypoints": "|".join(f"{p.lat},{p.lon}" for p in request.points), "mode": "driving"}
    if request.traffic_mode == "off":
        params["traffic"] = "disabled"
    elif request.departure_at is not None:
        if request.departure_at < now:
            raise ProviderError(422, "invalid_departure_time", "Traffic forecast departure must not be in the past")
        params["departure_time"] = math.ceil(request.departure_at.timestamp())
    # Omit results: it selects a different response schema and may need a paid
    # tariff. Do not send undocumented traffic=enabled or optimize waypoints.
    return params


def parse_route(data: dict, request: RoadRouteRequest, now: datetime) -> RoadRouteResponse:
    try:
        traffic_type = data["traffic_type"]
        if traffic_type not in ("realtime", "forecast", "disabled"):
            raise ValueError("Unknown traffic type")
        if (request.traffic_mode == "off") != (traffic_type == "disabled"):
            raise ValueError("Traffic response does not match request")
        legs = data["route"]["legs"]
        if not isinstance(legs, list) or len(legs) != len(request.points) - 1:
            raise ValueError("Unexpected leg count")
        statuses = [leg["status"] for leg in legs]
        if any(status not in ("OK", "FAIL") for status in statuses):
            raise ValueError("Unknown route status")
        common = dict(mode="yandex", traffic_mode=request.traffic_mode, traffic_type=traffic_type,
                      departure_at=request.departure_at if request.traffic_mode == "on" else None,
                      calculated_at=now)
        if "FAIL" in statuses:
            return RoadRouteResponse(**common, route=None,
                                     warnings=[DRIVING_WARNING, "Yandex could not build the requested road route"])
        distance, duration = 0.0, 0.0
        coordinates = []
        for leg in legs:
            steps = leg["steps"]
            if not isinstance(steps, list) or not steps:
                raise ValueError("Missing route steps")
            for step in steps:
                if step["mode"] != "driving":
                    raise ValueError("Unexpected travel mode")
                # Validate each value before summing: a negative step must not
                # be hidden by another positive one. bool is not a measurement.
                for key in ("length", "duration"):
                    value = step[key]
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("Invalid measurement")
                points = step["polyline"]["points"]
                if not isinstance(points, list) or len(points) < 2:
                    raise ValueError("Invalid step geometry")
                line = []
                for pair in points:
                    if not isinstance(pair, list) or len(pair) != 2:
                        raise ValueError("Invalid position")
                    lat, lon = pair
                    if any(type(v) not in (int, float) or not math.isfinite(v) for v in pair):
                        raise ValueError("Invalid coordinate")
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError("Coordinate outside WGS84")
                    line.append((lon, lat))
                if coordinates and coordinates[-1] != line[0]:
                    raise ValueError("Disconnected step geometry")
                coordinates.extend(line[1:] if coordinates else line)
                distance += step["length"]
                duration += step["duration"]
        route = RoadRoute(distance_meters=distance, duration_seconds=duration,
                          geometry={"type": "LineString", "coordinates": coordinates})
        # waiting_duration is deprecated. Do not add it to duration a second time.
        return RoadRouteResponse(**common, route=route, warnings=[DRIVING_WARNING])
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError, ValidationError):
        raise ProviderError(502, "router_provider_error", "Invalid route response from Yandex provider") from None


class YandexRouter(YandexClient):
    error_prefix = "router_provider"
    
    def __init__(self, client: httpx.Client | None = None):
        super().__init__(client)
        self.cache = {}

    def cache_key(self, request: RoadRouteRequest) -> str:
        raw = "|".join(f"{p.lat},{p.lon}" for p in request.points)
        raw += f"|{request.traffic_mode}"
        if request.departure_at is not None:
            raw += f"|{request.departure_at.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def cache_get(self, request: RoadRouteRequest) -> RoadRouteResponse | None:
        key = self.cache_key(request)
        entry = self.cache.get(key)
        if entry is None:
            return None
        route, cached_at = entry
        if datetime.now(timezone.utc).timestamp() - cached_at > CACHE_ENTRY_LIFETIME:
            del self.cache[key]
            return None
        return route

    def cache_put(self, request: RoadRouteRequest, response: RoadRouteResponse) -> None:
        key = self.cache_key(request)
        self.cache[key] = (response, datetime.now(timezone.utc).timestamp())

    def estimate(self, request: RoadRouteRequest) -> RoadRouteResponse:
        cached = self.cache_get(request)
        if cached is not None:
            return cached
        now = datetime.now(timezone.utc)
        params = driving_params(request, now)
        data = self._get(ROUTER_URL, "YANDEX_ROUTER_API_KEY", params)
        response = parse_route(data, request, datetime.now(timezone.utc))
        self.cache_put(request, response)
        return response


def dummy_road_route(request: RoadRouteRequest) -> RoadRouteResponse:
    a, b = request.points
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    h = math.sin((lat2 - lat1) / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b.lon - a.lon) / 2)**2
    distance = 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, h))))
    return RoadRouteResponse(mode="dummy", traffic_mode=request.traffic_mode,
        traffic_type=None, departure_at=request.departure_at if request.traffic_mode == "on" else None,
        calculated_at=datetime.now(timezone.utc),
        route=RoadRoute(distance_meters=round(distance, 2), duration_seconds=round(distance / 10, 2),
                        geometry={"type": "LineString", "coordinates": [(a.lon, a.lat), (b.lon, b.lat)]}),
        warnings=["Synthetic straight line at a fixed speed; no real road or traffic data; no external API was called", DRIVING_WARNING])
