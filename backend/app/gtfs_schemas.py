"""Read-only GTFS contract; these are lines and trips, not A-to-B planning."""
from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from .schemas import LineString, Model, Point

GtfsTransportMode = Literal['bus', 'tram', 'trolleybus', 'metro', 'rail', 'unknown']


class GtfsStop(Point):
    stop_id: str
    name: str
    code: str | None = None
    description: str | None = None
    parent_station: str | None = None
    zone_id: str | None = None
    location_type: int = Field(default=0, ge=0, le=4)


class GtfsRoute(Model):
    route_id: str
    agency_id: str | None = None
    short_name: str
    long_name: str
    description: str | None = None
    mode: GtfsTransportMode
    route_type: int = Field(ge=0)
    color: str | None = Field(default=None, pattern=r'^[0-9A-Fa-f]{6}$')
    text_color: str | None = Field(default=None, pattern=r'^[0-9A-Fa-f]{6}$')


class GtfsDirection(Model):
    direction_id: Literal[0, 1] | None
    headsign: str | None


class GtfsTripSummary(Model):
    trip_id: str
    route_id: str
    service_id: str
    headsign: str | None
    direction_id: Literal[0, 1] | None
    shape_id: str | None = None


class GtfsStopTime(Model):
    stop: GtfsStop
    stop_sequence: int = Field(ge=0)
    arrival_time: str | None = Field(pattern=r'^\d{2,}:[0-5]\d:[0-5]\d$')
    departure_time: str | None = Field(pattern=r'^\d{2,}:[0-5]\d:[0-5]\d$')
    pickup_type: Literal[0, 1, 2, 3] | None = None
    drop_off_type: Literal[0, 1, 2, 3] | None = None
    timepoint: Literal[0, 1] | None = None


class GtfsTrip(GtfsTripSummary):
    stop_times: list[GtfsStopTime]
    geometry: LineString | None


class GtfsDeparture(Model):
    trip_id: str
    service_id: str
    stop_id: str
    stop_sequence: int = Field(ge=0)
    line: GtfsRoute
    headsign: str | None
    service_date: date
    scheduled_departure_at: AwareDatetime | None
    departure_time: str | None = Field(pattern=r'^\d{2,}:[0-5]\d:[0-5]\d$')
    schedule_type: Literal['timetable', 'frequency'] = 'timetable'
    headway_seconds: int | None = Field(default=None, gt=0)
    frequency_start_at: AwareDatetime | None = None
    frequency_end_at: AwareDatetime | None = None
    time_source: Literal['schedule'] = 'schedule'


class GtfsResponse(Model):
    mode: Literal['dummy', 'gtfs']
    gtfs_version: str | None
    warnings: list[str] = Field(default_factory=list)


class GtfsPage(GtfsResponse):
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    total: int = Field(ge=0)
    has_more: bool


class GtfsStopsResponse(GtfsPage):
    items: list[GtfsStop]


class GtfsStopResponse(GtfsResponse):
    stop: GtfsStop
    routes: list[GtfsRoute]


class GtfsRoutesResponse(GtfsPage):
    items: list[GtfsRoute]


class GtfsRouteResponse(GtfsPage):
    line: GtfsRoute
    directions: list[GtfsDirection]
    trips: list[GtfsTripSummary]


class GtfsTripResponse(GtfsResponse):
    trip: GtfsTrip


class GtfsDeparturesResponse(GtfsResponse):
    stop_id: str
    departure_at: AwareDatetime
    lookahead_minutes: int = Field(ge=1, le=1440)
    limit: int = Field(ge=1, le=100)
    has_more: bool
    items: list[GtfsDeparture]
