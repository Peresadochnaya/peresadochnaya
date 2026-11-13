"""Coherent demo itineraries from the same small GTFS as stops and vehicles.

This fixture composer is not a transit planner. It offers at most one bus from
our two bundled patterns, with an explicitly synthetic ten-minute timetable.
"""
import math
from datetime import timedelta
from pathlib import Path

from .gtfs_store import GtfsError, GtfsStore, seconds
from .schemas import Leg, Place, Route, RouteProvenance, RouteRequest, TransitLine


def distance(a, b):
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    h = math.sin((lat2 - lat1) / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b.lon - a.lon) / 2)**2
    return 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def compose(request: RouteRequest) -> Route:
    origin = Place(**request.points[0].model_dump(), name="Начало маршрута")
    destination = Place(**request.points[1].model_dump(), name="Конец маршрута")
    speed = {"slow": 1.0, "normal": 1.4, "fast": 1.8}[request.walk_speed]
    # For an arrival search the demo composes a route backwards from a
    # conservative two-hour window, then aligns it to the requested arrival.
    anchor = request.departure_at if request.departure_at is not None else request.arrival_at - timedelta(hours=2)
    start = anchor + timedelta(microseconds=(1000000 - anchor.microsecond) % 1000000)
    current, legs = start, []

    def append(mode, a, b, duration, coordinates=None, line=None):
        nonlocal current
        coords = coordinates or [(a.lon, a.lat), (b.lon, b.lat)]
        length = sum(distance(Place(lon=x[0], lat=x[1], name=""), Place(lon=y[0], lat=y[1], name=""))
                     for x, y in zip(coords, coords[1:]))
        end = current + timedelta(seconds=duration)
        legs.append(Leg(mode=mode, origin=a, destination=b, start_at=current, end_at=end,
                        duration_seconds=duration, original_duration_seconds=duration, duration_source="dummy",
                        distance_meters=round(length, 2), line=line,
                        geometry={"type": "LineString", "coordinates": coords}))
        current = end

    allowed = (request.modes is None or "bus" in request.modes) and "bus" not in (request.exclude_modes or [])
    candidate = None
    if allowed and distance(origin, destination) > 1:
        with GtfsStore(Path("/unused")).open(dummy=True) as snapshot:
            for trip_id in sorted(snapshot.trips):
                trip, _ = snapshot.trip(trip_id)
                calls = trip.stop_times
                for i, first in enumerate(calls[:-1]):
                    if first.pickup_type == 1 or not first.departure_time or "ПОСАДКИ И ВЫСАДКИ НЕТ" in first.stop.name:
                        continue
                    if distance(origin, first.stop) > 20000:
                        continue
                    for j in range(i + 1, len(calls)):
                        last = calls[j]
                        if last.drop_off_type == 1 or not last.arrival_time or "ПОСАДКИ И ВЫСАДКИ НЕТ" in last.stop.name:
                            continue
                        if distance(last.stop, destination) > 20000:
                            continue
                        ride = seconds(last.arrival_time) - seconds(first.departure_time)
                        if ride <= 0:
                            continue
                        walk = distance(origin, first.stop) + distance(last.stop, destination)
                        score = walk + ride * speed
                        if candidate is None or score < candidate[0]:
                            candidate = (score, trip, first, last, ride)
            if candidate is not None and candidate[0] < distance(origin, destination):
                _, trip, first, last, ride = candidate
                access = math.ceil(distance(origin, first.stop) / speed)
                ready = current + timedelta(seconds=access)
                try:
                    departures, _, _ = snapshot.departures(first.stop.stop_id, ready, 60, 100)
                except GtfsError:
                    departures = []
                departure = next((d for d in departures if d.trip_id == trip.trip_id and d.scheduled_departure_at), None)
                if departure is not None:
                    board = Place(**first.stop.model_dump(include={"lat", "lon", "name", "stop_id"}))
                    alight = Place(**last.stop.model_dump(include={"lat", "lon", "name", "stop_id"}))
                    append("walk", origin, board, access)
                    wait = math.ceil((departure.scheduled_departure_at - current).total_seconds())
                    append("wait", board, board, wait)
                    # Use the recorded road shape between the selected stops.
                    shape = list(trip.geometry.coordinates) if trip.geometry else []
                    if shape:
                        begin = min(range(len(shape)), key=lambda k:(shape[k][0]-board.lon)**2+(shape[k][1]-board.lat)**2)
                        end = min(range(begin, len(shape)), key=lambda k:(shape[k][0]-alight.lon)**2+(shape[k][1]-alight.lat)**2)
                        shape = shape[begin:end + 1]
                    coords = [(board.lon, board.lat), *shape, (alight.lon, alight.lat)]
                    line = snapshot.routes[trip.route_id]
                    append("bus", board, alight, ride, coords,
                           TransitLine(short_name=line.short_name, route_id=trip.route_id, trip_id=trip.trip_id))
                    append("walk", alight, destination, math.ceil(distance(alight, destination) / speed))
    if not legs:
        append("walk", origin, destination, math.ceil(distance(origin, destination) / speed))
    coords = list(legs[0].geometry.coordinates)
    for leg in legs[1:]:
        coords.extend(leg.geometry.coordinates[1:])
    route = Route(id="dummy:route-193", name="Демонстрационный маршрут", distance_meters=round(sum(x.distance_meters for x in legs), 2),
                  geometry={"type": "LineString", "coordinates": coords}, start_at=start, end_at=current,
                  duration_seconds=sum(x.duration_seconds for x in legs), transfers=0, legs=legs,
                  provenance=RouteProvenance(candidate_providers=[], traffic_providers=[]))
    if request.arrival_at is not None:
        delta = request.arrival_at - route.end_at
        route = route.model_copy(update={
            "start_at": route.start_at + delta,
            "end_at": request.arrival_at,
            "legs": [
                leg.model_copy(update={
                    "start_at": leg.start_at + delta,
                    "end_at": leg.end_at + delta,
                })
                for leg in route.legs
            ],
        })
    return route
