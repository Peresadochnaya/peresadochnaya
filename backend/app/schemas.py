from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Point(Model):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


TicketProfile = Literal["full_fare", "podorozhnik_wallet", "monthly_unified", "monthly_surface", "monthly_bus", "monthly_metro", "student_unified", "student_surface", "student_metro", "school_unified", "school_bus", "concession_unified"]


class Passenger(Model):
    ticket_profile: TicketProfile = "full_fare"


class TicketProfileInfo(Model):
    id: TicketProfile
    label_ru: str
    coverage_status: Literal["not_implemented"] = "not_implemented"


class RouteRequest(Model):
    points: list[Point] = Field(min_length=2, max_length=2,
                                description="Ordered route points; currently exactly origin and destination")
    passenger: Passenger = Field(default_factory=Passenger)
    departure_at: AwareDatetime
    traffic_mode: Literal["off", "on"] = "off"
    max_results: int = Field(default=5, ge=1, le=10)


class Place(Point):
    name: str
    stop_id: str | None = None


class TransitLine(Model):
    short_name: str
    route_id: str
    trip_id: str | None = None


class Leg(Model):
    mode: Literal["walk", "bus", "tram", "trolleybus", "metro", "rail", "wait"]
    origin: Place = Field(alias="from")
    destination: Place = Field(alias="to")
    start_at: AwareDatetime
    end_at: AwareDatetime
    duration_seconds: int = Field(ge=0)
    original_duration_seconds: int = Field(ge=0)
    duration_source: Literal["dummy", "schedule", "yandex"]
    line: TransitLine | None = None
    # GeoJSON LineString coordinate order: longitude, latitude.
    geometry: dict


class Route(Model):
    id: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    duration_seconds: int = Field(ge=0)
    transfers: int = Field(ge=0)
    legs: list[Leg]


class RoutesResponse(Model):
    mode: Literal["dummy", "planner"]
    passenger: Passenger
    routes: list[Route]
    gtfs_version: str | None
    graph_version: str | None
    calculated_at: AwareDatetime
    warnings: list[str]


class ErrorDetail(Model):
    code: str
    message: str


class ErrorResponse(Model):
    error: ErrorDetail


class ValidationErrorResponse(BaseModel):
    detail: list[dict]


class DataStatus(Model):
    state: Literal["missing", "ready", "degraded"]
    gtfs_version: str | None = None
    graph_version: str | None = None
    last_check: dict | None = None
    manifest: dict | None = None
    warnings: list[str] = Field(default_factory=list)
