"""OpenTripPlanner (OTP 2.x) GraphQL adapter.

Dedicated walking and transit searches, no retries and no redirects. The adapter
maps OTP itineraries to the public ``Route``/``Leg`` contract and keeps OTP
identifiers (route, trip, stop) for later use.
"""
import hashlib
import math
import os
import time
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError

from .schemas import (FareItem, FarePreferences, Leg, LineString, Passenger, Place, Route, RouteFare,
                      RouteProvenance, RouteRequest, RoutesResponse, RoutesResponseSources,
                      TransitLine)
from .yandex import ProviderError
from .route_selection import is_walking_route

DEFAULT_URL = "http://localhost:8080"
DEFAULT_TIMEOUT = 20.0

# Only modes represented by the public Leg contract are requested. Direct
# street search stays walking-only; bicycles, cars and taxis are not planned.
PLAN_QUERY = """
query Plan($origin: PlanLabeledLocationInput!, $destination: PlanLabeledLocationInput!,
           $dateTime: PlanDateTimeInput!, $first: Int!, $modes: PlanModesInput,
           $preferences: PlanPreferencesInput) {
  planConnection(origin: $origin, destination: $destination, dateTime: $dateTime,
                 first: $first, modes: $modes, preferences: $preferences) {
    routingErrors { code description inputField }
    edges {
      node {
        start
        end
        duration
        numberOfTransfers
        legs {
          mode
          transitLeg
          distance
          start { scheduledTime estimated { time delay } }
          end { scheduledTime estimated { time delay } }
          from { name lat lon stop { gtfsId name } }
          to { name lat lon stop { gtfsId name } }
          route { gtfsId shortName longName mode }
          trip { gtfsId }
          legGeometry { points length }
        }
      }
    }
  }
}
"""

TRANSIT_MODES = ["BUS", "TRAM", "TROLLEYBUS", "SUBWAY", "RAIL"]

# OTP exposes only a time multiplier per transit mode (cost.reluctance), not an
# additive fare. A fare is therefore converted into seconds of generalized cost
# using the passenger's value of time, and those seconds are spread over a
# reference transit leg. The result ranks expensive modes lower without
# pretending to reproduce an exact ticket price during the search.
TYPICAL_LEG_SECONDS = 900.0
MAX_RELUCTANCE = 20.0

MODE_FARE = {
    "BUS": "bus",
    "TRAM": "tram",
    "TROLLEYBUS": "trolleybus",
    "SUBWAY": "metro",
    "RAIL": "rail",
}

WALK_SPEED_METERS_PER_SECOND = {"slow": 0.9, "normal": 1.3, "fast": 1.7}
REQUEST_MODE = {value: key for key, value in MODE_FARE.items()}

MODE_MAP = {
    "WALK": "walk",
    "BUS": "bus",
    "COACH": "bus",
    "TROLLEYBUS": "trolleybus",
    "TRAM": "tram",
    "SUBWAY": "metro",
    "RAIL": "rail",
}

MODE_LABEL = {
    "walk": "Пешком",
    "bus": "Автобус",
    "tram": "Трамвай",
    "trolleybus": "Троллейбус",
    "metro": "Метро",
    "rail": "Электричка",
}

TICKET_WARNING = ("Ticket profile is recorded only; fare, eligibility and pass coverage "
                  "are not calculated")
FARE_WARNING = ("Fare is calculated from the explicit tariff rules in the request; "
                "the ticket profile is not used")
FARE_SEARCH_WARNING = ("Mode fares influence the search as time multipliers, so the "
                       "returned itinerary is not guaranteed to be the cheapest one")
TRAFFIC_WARNING = ("traffic_mode is not applied to public transport planning; "
                   "OTP scheduled times are used")


def decode_polyline(value: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode an encoded polyline into GeoJSON-ordered (lon, lat) positions."""
    factor = 10 ** precision
    index = latitude = longitude = 0
    coordinates: list[tuple[float, float]] = []
    while index < len(value):
        for is_longitude in (False, True):
            result = shift = 0
            while True:
                if index >= len(value):
                    raise ValueError("Truncated polyline")
                byte = ord(value[index]) - 63
                index += 1
                if byte < 0:
                    raise ValueError("Invalid polyline character")
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_longitude:
                longitude += delta
            else:
                latitude += delta
        lat, lon = latitude / factor, longitude / factor
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Polyline coordinate outside WGS84")
        coordinates.append((lon, lat))
    return coordinates


def _parse_time(moment: str) -> datetime:
    parsed = datetime.fromisoformat(moment)
    if parsed.tzinfo is None:
        raise ValueError("Naive time from OTP")
    return parsed


def _time(value: dict | None, fallback: str | None = None, *, scheduled_only: bool = False) -> datetime:
    """Prefer the real-time estimate, then the scheduled time."""
    if isinstance(value, dict):
        estimated = value.get("estimated") or {}
        moment = estimated.get("time") if isinstance(estimated, dict) else None
        if not scheduled_only and isinstance(moment, str) and moment:
            return _parse_time(moment)
        fallback = value.get("scheduledTime", fallback)
    if isinstance(fallback, str) and fallback:
        return _parse_time(fallback)
    raise ValueError("Missing leg time")


def _place(value: dict) -> Place:
    lat, lon = value.get("lat"), value.get("lon")
    if type(lat) not in (int, float) or type(lon) not in (int, float):
        raise ValueError("Missing place coordinates")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Place coordinate outside WGS84")
    stop = value.get("stop") or {}
    return Place(lat=lat, lon=lon,
                 name=value.get("name") or "Точка на карте",
                 stop_id=stop.get("gtfsId"))


def _line(value: list[tuple[float, float]]) -> LineString:
    coordinates: list[tuple[float, float]] = []
    for lon, lat in value:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("Geometry outside WGS84")
        if not coordinates or coordinates[-1] != (lon, lat):
            coordinates.append((lon, lat))
    if len(coordinates) == 1:
        coordinates.append(coordinates[0])
    if len(coordinates) < 2:
        raise ValueError("Empty geometry")
    return LineString(coordinates=coordinates)


def _leg_geometry(leg: dict, origin: Place, destination: Place) -> LineString:
    encoded = (leg.get("legGeometry") or {}).get("points")
    if isinstance(encoded, str) and encoded:
        coordinates = decode_polyline(encoded)
    else:
        coordinates = [(origin.lon, origin.lat), (destination.lon, destination.lat)]
    if len(coordinates) == 1:
        coordinates.append(coordinates[0])
    return _line(coordinates)


def _leg_mode(leg: dict) -> str:
    mode = leg.get("mode")
    if mode not in MODE_MAP:
        raise ValueError(f"Unsupported OTP mode: {mode}")
    return MODE_MAP[mode]


def _transit_line(leg: dict) -> TransitLine | None:
    if not leg.get("transitLeg"):
        return None
    route = leg.get("route") or {}
    trip = leg.get("trip") or {}
    route_id = route.get("gtfsId")
    if not route_id:
        raise ValueError("Transit leg without route id")
    return TransitLine(short_name=route.get("shortName") or route.get("longName") or route_id,
                       route_id=route_id, trip_id=trip.get("gtfsId"))


def mode_reluctance(cost_rub: float, value_of_time_rub_per_hour: float) -> float:
    """Turn a per-boarding fare into OTP's time multiplier.

    ``cost_rub`` rubles are worth ``cost_rub * 3600 / value_of_time`` seconds of
    travel time. Spreading that penalty over a reference leg keeps the multiplier
    independent of the actual leg duration, which is unknown before the search.
    """
    if cost_rub <= 0:
        return 1.0
    penalty_seconds = cost_rub * 3600.0 / value_of_time_rub_per_hour
    return round(min(MAX_RELUCTANCE, 1.0 + penalty_seconds / TYPICAL_LEG_SECONDS), 3)


SURFACE_MODES = {"bus", "tram", "trolleybus"}
CITY_2026_BANK = {"bus": 88.0, "tram": 88.0, "trolleybus": 88.0, "metro": 95.0, "rail": 65.0}
CITY_2026_PODOROZHNIK = {mode: 65.0 for mode in CITY_2026_BANK}
PASS_COVERAGE = {
    "unlimited_surface": SURFACE_MODES,
    "unlimited_metro": {"metro"},
    "unlimited_all": SURFACE_MODES | {"metro"},
}


def route_fare(legs: list[Leg], preferences: FarePreferences | None,
               passenger: Passenger | None = None) -> RouteFare:
    """Calculate the 2026 St Petersburg passenger fare after OTP has planned.

    Explicit ``fare`` rules override a profile's base prices.  They do not
    disable an unlimited pass or the Podorozhnik 60-minute surface transfer.
    """
    passenger = passenger or Passenger()
    custom_prices = {rule.mode: rule.cost_rub for rule in preferences.rules} if preferences else {}
    payment = passenger.uncovered_payment if passenger.ticket_profile.startswith("unlimited_") else passenger.ticket_profile
    base = CITY_2026_PODOROZHNIK if payment == "podorozhnik_wallet" else CITY_2026_BANK
    coverage = PASS_COVERAGE.get(passenger.ticket_profile, set())
    costs: dict[str, float] = {}
    rides: dict[str, int] = {}
    transfer_start: datetime | None = None
    surface_rides = 0
    transit_legs = [leg for leg in legs if leg.line is not None]
    for leg in transit_legs:
        rides[leg.mode] = rides.get(leg.mode, 0) + 1
        if leg.mode in custom_prices:
            cost = custom_prices[leg.mode]
        elif leg.mode in coverage:
            cost = 0.0
        elif payment == "podorozhnik_wallet" and leg.mode in SURFACE_MODES:
            if transfer_start is None or (leg.start_at - transfer_start).total_seconds() > 3600:
                transfer_start, surface_rides = leg.start_at, 0
            cost = (65.0, 14.0, 0.0)[min(surface_rides, 2)]
            surface_rides += 1
        else:
            cost = base[leg.mode]
            # Metro and rail terminate a Podorozhnik surface transfer chain.
            if leg.mode not in SURFACE_MODES:
                transfer_start, surface_rides = None, 0
        costs[leg.mode] = round(costs.get(leg.mode, 0.0) + cost, 2)
    items = [FareItem(mode=mode, rides=count, cost_rub=costs.get(mode, 0.0))
             for mode, count in sorted(rides.items())]
    transfers = max(0, len(transit_legs) - 1)
    transfer_cost = preferences.transfer_cost_rub if preferences else 0.0
    return RouteFare(amount_rub=round(sum(costs.values()) + transfers * transfer_cost, 2),
                     items=items, transfers=transfers, transfer_cost_rub=transfer_cost)


def _wait_leg(start: datetime, end: datetime, place: Place) -> Leg | None:
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        return None
    point = (place.lon, place.lat)
    return Leg(mode="wait", origin=place, destination=place, start_at=start, end_at=end,
               duration_seconds=seconds, original_duration_seconds=seconds,
               duration_source="schedule", distance_meters=0.0,
               geometry=LineString(coordinates=[point, point]))


def _route_name(legs: list[Leg]) -> str:
    labels = []
    for leg in legs:
        if leg.mode == "wait" or leg.line is None:
            continue
        labels.append(f"{MODE_LABEL[leg.mode]} {leg.line.short_name}".strip())
    return " → ".join(labels) if labels else "Пешеходный маршрут"


def _route_id(legs: list[Leg], start: datetime) -> str:
    payload = "|".join(
        f"{leg.mode}:{leg.line.route_id if leg.line else ''}:"
        f"{leg.line.trip_id if leg.line else ''}:{leg.start_at.isoformat()}"
        for leg in legs
    ) + f"|{start.isoformat()}"
    return "otp:" + hashlib.sha1(payload.encode()).hexdigest()[:16]


def _itinerary(node: dict, fare: FarePreferences | None, passenger: Passenger, *,
               scheduled_only: bool = False) -> tuple[Route, bool]:
    raw_legs = node.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        raise ValueError("Itinerary without legs")

    legs: list[Leg] = []
    has_transit = False
    previous_end: datetime | None = None
    previous_place: Place | None = None
    distance = 0.0

    for raw in raw_legs:
        origin = _place(raw.get("from") or {})
        destination = _place(raw.get("to") or {})
        start = _time(raw.get("start"), scheduled_only=scheduled_only)
        end = _time(raw.get("end"), scheduled_only=scheduled_only)
        if end < start:
            raise ValueError("Leg ends before it starts")
        if previous_end is not None and previous_place is not None:
            wait = _wait_leg(previous_end, start, previous_place)
            if wait is not None:
                legs.append(wait)
        mode = _leg_mode(raw)
        line = _transit_line(raw)
        has_transit = has_transit or line is not None
        leg_distance = raw.get("distance")
        if leg_distance is None:
            leg_distance = 0.0
        if type(leg_distance) not in (int, float) or not math.isfinite(leg_distance) or leg_distance < 0:
            raise ValueError("Invalid leg distance")
        duration = int((end - start).total_seconds())
        legs.append(Leg(mode=mode, origin=origin, destination=destination,
                        start_at=start, end_at=end, duration_seconds=duration,
                        original_duration_seconds=duration, duration_source="schedule",
                        distance_meters=float(leg_distance),
                        line=line, geometry=_leg_geometry(raw, origin, destination)))
        distance += float(leg_distance)
        previous_end, previous_place = end, destination

    start_at = _time(None, node.get("start"))
    end_at = _time(None, node.get("end"))
    if end_at < start_at:
        raise ValueError("Itinerary ends before it starts")
    duration = node.get("duration")
    duration_seconds = int(duration) if type(duration) in (int, float) and duration >= 0 \
        else int((end_at - start_at).total_seconds())
    if scheduled_only:
        start_at, end_at = legs[0].start_at, legs[-1].end_at
        duration_seconds = int((end_at - start_at).total_seconds())
    transfers = node.get("numberOfTransfers")
    if type(transfers) is not int or transfers < 0:
        raise ValueError("Invalid transfer count")

    coordinates: list[tuple[float, float]] = []
    for leg in legs:
        for position in leg.geometry.coordinates:
            if not coordinates or coordinates[-1] != position:
                coordinates.append(position)

    route = Route(id=_route_id(legs, start_at), name=_route_name(legs),
                  distance_meters=round(distance, 2),
                  geometry=_line(coordinates), start_at=start_at, end_at=end_at,
                  duration_seconds=duration_seconds, transfers=transfers, legs=legs,
                  provenance=RouteProvenance(candidate_providers=["otp"], traffic_providers=[]),
                  fare=route_fare(legs, fare, passenger))
    return route, has_transit


class OtpPlanner:
    error_prefix = "planner"

    def __init__(self, url: str | None = None, timeout: float | None = None,
                 client: httpx.Client | None = None):
        self.url = (url or os.getenv("OTP_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout or float(os.getenv("OTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT))
        self.client = client

    def _post(self, payload: dict, timeout_seconds: float | None = None) -> dict:
        timeout = min(self.timeout, timeout_seconds) if timeout_seconds is not None else self.timeout
        try:
            if self.client is not None:
                response = self.client.post(f"{self.url}/otp/gtfs/v1", json=payload,
                                            follow_redirects=False, timeout=timeout)
            else:
                with httpx.Client(timeout=timeout, follow_redirects=False,
                                  trust_env=False) as client:
                    response = client.post(f"{self.url}/otp/gtfs/v1", json=payload)
        except httpx.TimeoutException:
            raise ProviderError(504, f"{self.error_prefix}_timeout",
                                "Route planner timed out") from None
        except httpx.HTTPError:
            raise ProviderError(503, f"{self.error_prefix}_unavailable",
                                "Route planner is unavailable") from None
        if response.status_code != 200:
            raise ProviderError(502, f"{self.error_prefix}_error",
                                "Route planner rejected the request")
        try:
            body = response.json()
        except ValueError:
            raise ProviderError(502, f"{self.error_prefix}_error",
                                "Invalid response from route planner") from None
        if not isinstance(body, dict):
            raise ProviderError(502, f"{self.error_prefix}_error",
                                "Invalid response from route planner")
        if body.get("errors"):
            raise ProviderError(502, f"{self.error_prefix}_error",
                                "Route planner returned GraphQL errors")
        return body

    def plan(self, request: RouteRequest, *, gtfs_version: str | None = None,
             graph_version: str | None = None, timeout_seconds: float | None = None,
             origin_stop_id: str | None = None, exclude_realtime_updates: bool = False,
             ensure_walking: bool = True) -> RoutesResponse:
        options = dict(gtfs_version=gtfs_version, graph_version=graph_version,
                       origin_stop_id=origin_stop_id,
                       exclude_realtime_updates=exclude_realtime_updates)
        transit = (set(request.modes or REQUEST_MODE) - set(request.exclude_modes or [])) & set(REQUEST_MODE)
        budget = min(self.timeout, timeout_seconds) if timeout_seconds is not None else self.timeout
        started = time.monotonic()
        primary_budget = budget * 0.8 if ensure_walking and transit else budget
        response = self._plan_single(request, timeout_seconds=primary_budget, **options)
        response.routes = response.routes[:request.max_results]
        if response.routes or not ensure_walking or not transit:
            return response
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            response.warnings.append("Walking fallback skipped: route search time budget exhausted")
            return response
        walk_request = request.model_copy(update={"modes": ["walk"], "exclude_modes": None, "max_results": 1})
        try:
            walking = self._plan_single(walk_request, timeout_seconds=remaining,
                                        **{**options, "origin_stop_id": None})
        except ProviderError as exc:
            response.warnings.append(f"Walking fallback failed: {exc.code}")
            return response
        response.routes = [route for route in walking.routes if is_walking_route(route)][:1]
        response.warnings = list(dict.fromkeys(response.warnings + walking.warnings))
        if response.routes:
            response.warnings = [warning for warning in response.warnings
                                 if not warning.startswith("Route planner found no route")]
        return response

    def _plan_single(self, request: RouteRequest, *, gtfs_version: str | None = None,
             graph_version: str | None = None, timeout_seconds: float | None = None,
             origin_stop_id: str | None = None,
             exclude_realtime_updates: bool = False) -> RoutesResponse:
        prices = {rule.mode: rule.cost_rub for rule in request.fare.rules} \
            if request.fare is not None else {}
        enabled_modes = set(request.modes or REQUEST_MODE)
        enabled_modes -= set(request.exclude_modes or [])
        transit_modes = []
        for mode in TRANSIT_MODES:
            public_mode = MODE_FARE[mode]
            if public_mode not in enabled_modes:
                continue
            preference = {"mode": mode}
            if request.fare is not None:
                preference["cost"] = {"reluctance": mode_reluctance(
                    prices.get(public_mode, 0.0), request.fare.value_of_time_rub_per_hour)}
            transit_modes.append(preference)

        preferences = {
            "street": {"walk": {"speed": WALK_SPEED_METERS_PER_SECOND[request.walk_speed],
                                "reluctance": 6.0 if request.less_walking else 4.0}},
            "transit": {"transfer": (
                {"maximumTransfers": request.max_transfers}
                if request.max_transfers is not None else {}
            )},
        }

        if exclude_realtime_updates:
            # Road correction requires the GTFS schedule, without the GPS-delay
            # feed already accounting for congestion along the same journey.
            preferences["transit"]["timetable"] = {"excludeRealTimeUpdates": True}

        # No allowed transit modes is an intentional walking-only request, not
        # a malformed public-transport search.
        modes = ({"direct": ["WALK"], "directOnly": True}
                 if not transit_modes else
                 {"transit": {"access": ["WALK"], "egress": ["WALK"],
                              "transfer": ["WALK"], "transit": transit_modes}})

        if transit_modes:
            modes["direct"] = ["WALK"]

        payload = {
            "query": PLAN_QUERY,
            "variables": {
                "origin": {"location": {"coordinate": {
                    "latitude": request.points[0].lat, "longitude": request.points[0].lon}}},
                "destination": {"location": {"coordinate": {
                    "latitude": request.points[1].lat, "longitude": request.points[1].lon}}},
                "dateTime": {"earliestDeparture": request.departure_at.isoformat()},
                "first": request.max_results,
                "preferences": preferences,
                "modes": modes,
            },
        }
        if origin_stop_id:
            payload["variables"]["origin"] = {"location": {"stopLocation": {"stopLocationId": origin_stop_id}}}
        body = self._post(payload, timeout_seconds) if timeout_seconds is not None else self._post(payload)
        try:
            connection = body["data"]["planConnection"]
            errors = connection.get("routingErrors") or []
            if not isinstance(errors, list):
                raise ValueError("Invalid routing errors")
            edges = connection.get("edges") or []
            if not isinstance(edges, list):
                raise ValueError("Invalid itinerary list")
            routes = []
            has_transit = False
            for edge in edges:
                node = (edge or {}).get("node")
                if not isinstance(node, dict):
                    raise ValueError("Invalid itinerary node")
                route, transit = _itinerary(node, request.fare, request.passenger,
                                            scheduled_only=exclude_realtime_updates)
                routes.append(route)
                has_transit = has_transit or transit
            # OTP ranks by generalized cost; the public API promises the
            # simpler, stable presentation order: fastest route first.
            routes.sort(key=lambda route: (route.duration_seconds, route.transfers, route.id))
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
            raise ProviderError(502, f"{self.error_prefix}_error",
                                "Invalid response from route planner") from None

        warnings = [FARE_WARNING if request.fare is not None else
                    "Fare is calculated from the selected 2026 ticket profile"]
        if request.fare is not None:
            warnings.append(FARE_SEARCH_WARNING)
            unpriced = sorted({leg.mode for route in routes for leg in route.legs
                               if leg.line is not None and leg.mode not in prices})
            if unpriced:
                warnings.append(f"No fare rule for mode(s) {', '.join(unpriced)}; "
                                "counted as 0 RUB")
        if request.traffic_mode == "on" and transit_modes:
            warnings.append(TRAFFIC_WARNING)
        for item in errors:
            code = (item or {}).get("code")
            description = (item or {}).get("description")
            if code:
                warnings.append(f"OTP {code}: {description or 'routing error'}")
        if not routes:
            warnings.append("Route planner found no route within its search limits; "
                            "the destination may be unreachable on the available street/transit graph")
        if routes and not has_transit:
            warnings.append("Route planner returned only walking options; no transit legs")

        return RoutesResponse(mode="real",
                              sources=RoutesResponseSources(candidate_providers=["otp"],
                                                            traffic_providers=[],
                                                            realtime="unavailable"),
                              passenger=request.passenger, routes=routes,
                              gtfs_version=gtfs_version, graph_version=graph_version,
                              calculated_at=datetime.now(timezone.utc), warnings=warnings)
