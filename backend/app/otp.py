"""OpenTripPlanner (OTP 2.x) GraphQL adapter.

One HTTP request per route search, no retries and no redirects. The adapter
maps OTP itineraries to the public ``Route``/``Leg`` contract and keeps OTP
identifiers (route, trip, stop) for later use.
"""
import hashlib
import math
import os
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError

from .schemas import (FareItem, FarePreferences, Leg, LineString, Place, Route, RouteFare,
                      RouteProvenance, RouteRequest, RoutesResponse, RoutesResponseSources,
                      TransitLine)
from .yandex import ProviderError

DEFAULT_URL = "http://localhost:8080"
DEFAULT_TIMEOUT = 20.0

# Only modes represented by the public Leg contract are requested. Direct
# street search stays walking-only; bicycles, cars and taxis are not planned.
PLAN_QUERY = """
query Plan($origin: PlanLabeledLocationInput!, $destination: PlanLabeledLocationInput!,
           $dateTime: PlanDateTimeInput!, $first: Int!, $modes: PlanModesInput) {
  planConnection(origin: $origin, destination: $destination, dateTime: $dateTime,
                 first: $first, modes: $modes) {
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


def _time(value: dict | None, fallback: str | None = None) -> datetime:
    """Prefer the real-time estimate, then the scheduled time."""
    if isinstance(value, dict):
        estimated = value.get("estimated") or {}
        moment = estimated.get("time") if isinstance(estimated, dict) else None
        if isinstance(moment, str) and moment:
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


def route_fare(legs: list[Leg], preferences: FarePreferences) -> RouteFare:
    """Charge one fare per transit boarding plus a fixed transfer fee."""
    prices = {rule.mode: rule.cost_rub for rule in preferences.rules}
    rides: dict[str, int] = {}
    for leg in legs:
        if leg.line is None:
            continue
        rides[leg.mode] = rides.get(leg.mode, 0) + 1
    items = [FareItem(mode=mode, rides=count,
                      cost_rub=round(prices.get(mode, 0.0) * count, 2))
             for mode, count in sorted(rides.items())]
    transfers = max(0, sum(rides.values()) - 1)
    amount = round(sum(item.cost_rub for item in items)
                   + transfers * preferences.transfer_cost_rub, 2)
    return RouteFare(amount_rub=amount, items=items, transfers=transfers,
                     transfer_cost_rub=preferences.transfer_cost_rub)


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


def _itinerary(node: dict, fare: FarePreferences | None = None) -> tuple[Route, bool]:
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
        start = _time(raw.get("start"))
        end = _time(raw.get("end"))
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
                  fare=route_fare(legs, fare) if fare is not None else None)
    return route, has_transit


class OtpPlanner:
    error_prefix = "planner"

    def __init__(self, url: str | None = None, timeout: float | None = None,
                 client: httpx.Client | None = None):
        self.url = (url or os.getenv("OTP_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout or float(os.getenv("OTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT))
        self.client = client

    def _post(self, payload: dict) -> dict:
        try:
            if self.client is not None:
                response = self.client.post(f"{self.url}/otp/gtfs/v1", json=payload,
                                            follow_redirects=False, timeout=self.timeout)
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=False,
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
             graph_version: str | None = None) -> RoutesResponse:
        prices = {rule.mode: rule.cost_rub for rule in request.fare.rules} \
            if request.fare is not None else {}
        transit_modes = []
        for mode in TRANSIT_MODES:
            preference = {"mode": mode}
            if request.fare is not None:
                preference["cost"] = {"reluctance": mode_reluctance(
                    prices.get(MODE_FARE[mode], 0.0), request.fare.value_of_time_rub_per_hour)}
            transit_modes.append(preference)

        payload = {
            "query": PLAN_QUERY,
            "variables": {
                "origin": {"location": {"coordinate": {
                    "latitude": request.points[0].lat, "longitude": request.points[0].lon}}},
                "destination": {"location": {"coordinate": {
                    "latitude": request.points[1].lat, "longitude": request.points[1].lon}}},
                "dateTime": {"earliestDeparture": request.departure_at.isoformat()},
                "first": request.max_results,
                "modes": {"transit": {"access": ["WALK"], "egress": ["WALK"],
                                      "transfer": ["WALK"],
                                      "transit": transit_modes}},
            },
        }
        body = self._post(payload)
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
                route, transit = _itinerary(node, request.fare)
                routes.append(route)
                has_transit = has_transit or transit
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
            raise ProviderError(502, f"{self.error_prefix}_error",
                                "Invalid response from route planner") from None

        warnings = [FARE_WARNING if request.fare is not None else TICKET_WARNING]
        if request.fare is not None:
            warnings.append(FARE_SEARCH_WARNING)
            unpriced = sorted({leg.mode for route in routes for leg in route.legs
                               if leg.line is not None and leg.mode not in prices})
            if unpriced:
                warnings.append(f"No fare rule for mode(s) {', '.join(unpriced)}; "
                                "counted as 0 RUB")
        if request.traffic_mode == "on":
            warnings.append(TRAFFIC_WARNING)
        for item in errors:
            code = (item or {}).get("code")
            description = (item or {}).get("description")
            if code:
                warnings.append(f"OTP {code}: {description or 'routing error'}")
        if routes and not has_transit:
            warnings.append("Route planner returned only walking options; no transit legs")

        return RoutesResponse(mode="real",
                              sources=RoutesResponseSources(candidate_providers=["otp"],
                                                            traffic_providers=[],
                                                            realtime="unavailable"),
                              passenger=request.passenger, routes=routes,
                              gtfs_version=gtfs_version, graph_version=graph_version,
                              calculated_at=datetime.now(timezone.utc), warnings=warnings)
