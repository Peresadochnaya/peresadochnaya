from datetime import date
from typing import Annotated, Literal

from pydantic import AnyUrl, AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .api_docs import ROUTE_WARNINGS_DESCRIPTION


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Point(Model):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


GeoProvider = Literal["yandex", "2gis"]


class Suggestion(Model):
    title: str = Field(description="Main display text")
    subtitle: str | None = Field(default=None, description="Address or area clarification")
    uri: str = Field(description="Opaque provider-specific identifier for geocoding; preserve provider and never fetch this as a URL")


class SuggestResponse(Model):
    mode: Literal["dummy", "yandex", "2gis"]
    provider: GeoProvider = Field(default="yandex", description="Выбранный геосервис. Совместимая dummy-структура не означает живое обращение.")
    items: list[Suggestion]
    warnings: list[str] = Field(default_factory=list)


class GeocodedPlace(Point):
    label: str
    precision: str | None = None


class GeocodeResponse(Model):
    mode: Literal["dummy", "yandex", "2gis"]
    provider: GeoProvider = Field(default="yandex", description="Выбранный геосервис. Совместимая dummy-структура не означает живое обращение.")
    items: list[GeocodedPlace]
    warnings: list[str] = Field(default_factory=list)


TicketProfile = Literal["podorozhnik_wallet", "unlimited_surface", "unlimited_metro", "unlimited_all", "bank_card"]

FareMode = Literal["bus", "tram", "trolleybus", "metro", "rail"]


class FareRule(Model):
    mode: FareMode
    cost_rub: float = Field(ge=0, le=100000, allow_inf_nan=False,
                            description="Fare charged for one boarding of this mode, in rubles; 0 means free")


class FarePreferences(Model):
    """Explicit tariffs for the search. They replace the ticket profile:
    the profile is only recorded, while these rules affect the itinerary.
    """
    rules: list[FareRule] = Field(min_length=1, max_length=5,
                                  description="One rule per transit mode; missing modes are treated as free")
    transfer_cost_rub: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False,
                                     description="Extra charge for every transfer between two transit legs")
    value_of_time_rub_per_hour: float = Field(default=600, gt=0, le=100000, allow_inf_nan=False,
                                              description="How many rubles one hour of travel time is worth; converts fares into routing cost")

    @field_validator("rules")
    @classmethod
    def unique_modes(cls, rules: list[FareRule]) -> list[FareRule]:
        modes = [rule.mode for rule in rules]
        if len(set(modes)) != len(modes):
            raise ValueError("Each transit mode may have only one fare rule")
        return rules


class Passenger(Model):
    ticket_profile: TicketProfile = "bank_card"
    uncovered_payment: Literal["podorozhnik_wallet", "bank_card"] = Field(default="bank_card",
        description="Payment for rides not covered by the selected unlimited pass; irrelevant for wallet-only or bank-card-only profiles")


class TicketProfileInfo(Model):
    id: TicketProfile
    label_ru: str
    coverage_status: Literal["not_implemented"] = "not_implemented"
    description_ru: str
    covered_modes: list[Literal["bus", "tram", "trolleybus", "metro"]] = Field(default_factory=list)
    transfer_window_seconds: int | None = Field(default=None, ge=0)


class RouteRequest(Model):
    points: list[Point] = Field(min_length=2, max_length=2,
                                description="Ordered route points; currently exactly origin and destination")
    passenger: Passenger = Field(default_factory=Passenger)
    departure_at: AwareDatetime | None = Field(default=None,
        description="Search routes departing at or after this time; exactly one of departure_at or arrival_at is required")
    arrival_at: AwareDatetime | None = Field(default=None,
        description="Search routes arriving by this time; exactly one of departure_at or arrival_at is required")
    traffic_mode: Literal["off", "on"] = "off"
    max_results: int = Field(default=5, ge=1, le=10)
    providers: list[Literal["otp", "yandex", "2gis"]] = Field(default_factory=lambda: ["otp"], min_length=1, max_length=3,
        description="Enabled candidate planners; MVP uses OTP only. Does not turn on live requests without dummy=false.")
    modes: list[Literal["bus", "tram", "trolleybus", "metro", "rail"]] | None = Field(default=None, min_length=1, max_length=5,
        description="Allowed public transport modes; null means all. Walking access remains possible.")
    exclude_modes: list[Literal["bus", "tram", "trolleybus", "metro", "rail"]] | None = Field(default=None, max_length=5)
    max_transfers: int | None = Field(default=None, ge=0, le=7)
    walk_speed: Literal["slow", "normal", "fast"] = Field(default="normal", description="Walking speed preset for the future OTP adapter")
    less_walking: bool = Field(default=False, description="Prefer less walking; does not prohibit walking access or transfers")
    fare: FarePreferences | None = Field(default=None, description="Per-mode tariffs; omitted means no fare influence on routing")

    @field_validator("providers", "modes", "exclude_modes")
    @classmethod
    def no_duplicate_options(cls, values):
        if values is not None and len(values) != len(set(values)):
            raise ValueError("Options must not contain duplicates")
        return values

    @model_validator(mode="after")
    def exactly_one_search_time(self):
        if (self.departure_at is None) == (self.arrival_at is None):
            raise ValueError("Exactly one of departure_at or arrival_at must be provided")
        return self


class Place(Point):
    name: str
    stop_id: str | None = None


class TransitLine(Model):
    short_name: str
    route_id: str
    trip_id: str | None = None


class LineString(Model):
    """WGS84 GeoJSON line; positions are [longitude, latitude]."""
    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[
        Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)],
        Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)],
    ]] = Field(min_length=2)


class RoadRouteRequest(Model):
    points: list[Point] = Field(min_length=2, max_length=2)
    departure_at: AwareDatetime | None = Field(default=None, description="Optional future departure for traffic forecast; ignored with traffic_mode=off")
    traffic_mode: Literal["off", "on"] = "on"


class RoadRoute(Model):
    distance_meters: float = Field(ge=0, allow_inf_nan=False)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False,
                                    description="Driving duration, not a public transport travel time")
    geometry: LineString


class RoadRouteResponse(Model):
    mode: Literal["dummy", "yandex", "2gis"]
    provider: GeoProvider = Field(default="yandex", description="Выбранный геосервис. Совместимая dummy-структура не означает живое обращение.")
    traffic_mode: Literal["off", "on"]
    traffic_type: Literal["realtime", "forecast", "disabled"] | None
    departure_at: AwareDatetime | None
    calculated_at: AwareDatetime
    route: RoadRoute | None = Field(description="Null when the selected provider reports a failed route; no partial routes are returned")
    warnings: list[str] = Field(default_factory=list)


class RouteProvenance(Model):
    candidate_providers: list[Literal["otp", "yandex", "2gis"]] = Field(
        json_schema_extra={"uniqueItems": True},
        description="Источники исходного варианта. После дедупликации их может быть несколько. Для реального маршрута минимум один; для dummy допускается пустой список.")
    traffic_providers: list[GeoProvider] = Field(
        json_schema_extra={"uniqueItems": True},
        description="Только фактически применённые источники дорожных поправок. В MVP OTP без поправок — пустой список.")

    @field_validator("candidate_providers", "traffic_providers")
    @classmethod
    def unique_providers(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Providers must not contain duplicates")
        return values


class RoutesResponseSources(RouteProvenance):
    realtime: Literal["gtfs_rt", "unavailable", "dummy"]


class MetroWaitEstimate(Model):
    """Оценка ожидания по опубликованным интервалам, не GTFS-расписание и не realtime.

    min/max интервалов недостаточно для точного математического ожидания. Время прибытия поезда не выдумывается.
    """
    line: str
    day_type: Literal["weekday", "saturday", "sunday"]
    period_start: str = Field(description="Локальное начало периода обслуживания, Europe/Moscow; может быть после полуночи.")
    period_end: str
    min_headway_seconds: int = Field(ge=1)
    max_headway_seconds: int = Field(ge=1)
    representative_headway_seconds: float = Field(gt=0, allow_inf_nan=False,
        description="Явно выбранный модельный интервал, например середина опубликованного диапазона. Это предположение, не измерение.")
    estimated_wait_seconds: float = Field(ge=0, allow_inf_nan=False,
        description="Половина representative_headway_seconds при модели случайного прихода. Не верхняя граница и не гарантированное время ожидания.")
    model: Literal["half_representative_headway"]
    source_url: AnyUrl
    source_valid_from: date
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent_estimate(self):
        if self.min_headway_seconds > self.max_headway_seconds:
            raise ValueError("Minimum headway must not exceed maximum")
        if self.estimated_wait_seconds != self.representative_headway_seconds / 2:
            raise ValueError("Wait estimate must be half the representative headway")
        return self


class Leg(Model):
    mode: Literal["walk", "bus", "tram", "trolleybus", "metro", "rail", "wait"]
    origin: Place = Field(alias="from")
    destination: Place = Field(alias="to")
    start_at: AwareDatetime
    end_at: AwareDatetime
    duration_seconds: int = Field(ge=0)
    original_duration_seconds: int = Field(ge=0, description="Исходная длительность из расписания или модели, до дорожных поправок")
    duration_source: Literal["dummy", "schedule", "yandex", "2gis", "estimated"]
    line: TransitLine | None = None
    # GeoJSON LineString coordinate order: longitude, latitude.
    geometry: LineString
    distance_meters: float = Field(ge=0, allow_inf_nan=False,
        description="Расстояние участка, для ожидания 0. Сумма по участкам равна distance_meters маршрута.")
    metro_wait_estimate: MetroWaitEstimate | None = Field(default=None,
        description="Только для моделируемого ожидания метро. Реальный departure timestamp по этим данным неизвестен; start_at/end_at такого участка обозначают модельную временную цепочку.")

    @model_validator(mode="after")
    def coherent_wait(self):
        if self.mode == "wait" and self.distance_meters != 0:
            raise ValueError("Waiting has zero distance")
        if self.metro_wait_estimate is not None and (self.mode != "wait" or self.duration_source != "estimated"):
            raise ValueError("Metro wait metadata requires an estimated waiting leg")
        return self


class FareItem(Model):
    mode: FareMode
    rides: int = Field(ge=0, description="Number of boardings of this mode")
    cost_rub: float = Field(ge=0, allow_inf_nan=False, description="rides × fare of this mode")


class RouteFare(Model):
    currency: Literal["RUB"] = "RUB"
    amount_rub: float = Field(ge=0, allow_inf_nan=False, description="Total for boardings and transfers")
    items: list[FareItem]
    transfers: int = Field(ge=0, description="Transfers between two transit legs charged by transfer_cost_rub")
    transfer_cost_rub: float = Field(ge=0, allow_inf_nan=False)


class Route(Model):
    id: str
    name: str = Field(min_length=1, description="Free-form display label, not a fixed route category")
    distance_meters: float = Field(ge=0, allow_inf_nan=False, description="Total length of all moving legs in meters")
    geometry: LineString = Field(description="Full route line in travel order; detailed sections remain in legs")
    start_at: AwareDatetime
    end_at: AwareDatetime
    duration_seconds: int = Field(ge=0)
    transfers: int = Field(ge=0)
    legs: list[Leg]
    provenance: RouteProvenance
    fare: RouteFare | None = Field(default=None, description="Calculated only when the request provides fare rules")


class RoutesResponse(Model):
    mode: Literal["dummy", "real"] = Field(description="dummy — демонстрационные данные; real — расчёт планировщика. Наличие пробок и realtime определяется отдельно по sources.")
    sources: RoutesResponseSources
    passenger: Passenger
    routes: list[Route]
    gtfs_version: str | None
    graph_version: str | None
    calculated_at: AwareDatetime
    warnings: list[str] = Field(description=ROUTE_WARNINGS_DESCRIPTION)


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
    graph: dict | None = None
    graph_check: dict | None = None
    last_check: dict | None = None
    manifest: dict | None = None
    warnings: list[str] = Field(default_factory=list)
