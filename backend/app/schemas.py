from datetime import date
from typing import Annotated, Literal, Any

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
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum number of routes, including walking")
    providers: list[Literal["otp", "yandex", "2gis"]] = Field(default_factory=lambda: ["otp"], min_length=1, max_length=3,
        description="Enabled candidate planners; MVP uses OTP only. Does not turn on live requests without dummy=false.")
    modes: list[Literal["walk", "bus", "tram", "trolleybus", "metro", "rail"]] | None = Field(default=None, min_length=1, max_length=6,
        description="Allowed transit modes; null means all transit. [walk] requests walking only. Walking competes equally with transit; an empty search triggers a dedicated walking search.")
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


# Diagnostics below describe the planned traffic pipeline. They are optional:
# null means not evaluated/available, never a measured zero delay or free road.
class TrafficCoverage(Model):
    eligible_distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Длина наземных участков, к которым применима дорожная поправка; null — применимость не оценена")
    matched_distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Длина уверенно сопоставленного покрытия, без повторного счёта альтернатив провайдера")
    usable_distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Часть применимой длины с пригодной дорожной оценкой и временной базой; одного геометрического совпадения недостаточно")
    usable_coverage_ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False,
        description="usable_distance_meters / eligible_distance_meters; высокая доля не подтверждает пересадки или непроверенный хвост")
    uncovered_distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    coverage_ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False,
        description="matched_distance_meters / eligible_distance_meters; null при неизвестной или нулевой базе")

    @model_validator(mode="after")
    def coverage_bounds(self):
        if self.eligible_distance_meters is not None:
            for value in (self.matched_distance_meters, self.usable_distance_meters, self.uncovered_distance_meters):
                if value is not None and value > self.eligible_distance_meters + 0.01:
                    raise ValueError("Coverage distance exceeds eligible distance")
        return self


class TrafficObservationDebug(Model):
    observation_id: str | None = None
    provider: GeoProvider
    traffic_type: Literal["realtime", "forecast", "disabled", "unknown"]
    observed_at: AwareDatetime | None = Field(default=None, description="Время получения исходной оценки, не время последнего чтения кэша")
    expires_at: AwareDatetime | None = None
    age_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    seconds_until_segment_entry: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Оценка времени до въезда на участок относительно calculated_at ответа, в секундах; не возраст кэша")
    observed_age_at_entry_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Ожидаемый возраст текущего наблюдения в момент въезда на участок; свежий кэш не гарантирует актуальность для далёкого въезда")
    cache_status: Literal["hit", "miss", "stale", "bypassed", "unknown"]
    freshness: Literal["fresh", "stale", "unknown"]
    driving_duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Автомобильное время только на геометрии этой оценки; не длительность автобуса")
    distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    provider_alternative_id: str | None = None
    provider_leg_index: int | None = Field(default=None, ge=0)
    provider_step_index: int | None = Field(default=None, ge=0,
        description="Индекс шага Яндекса или манёвра 2ГИС; duration относится ко всему шагу/манёвру")
    selected_for_correction: bool | None = Field(default=None,
        description="true только если оценка выбрана для поправки; получение данных не означает их применение")
    selection_reason: str | None = Field(default=None, description="Объяснение выбора/отклонения без URL, ключей и сырых запросов")

    @model_validator(mode="after")
    def valid_expiry(self):
        if self.observed_at is not None and self.expires_at is not None and self.expires_at < self.observed_at:
            raise ValueError("Observation expires before it was obtained")
        return self


class TrafficSegmentDebug(Model):
    lane_spans: list[dict[str, Any]] = Field(default_factory=list,description="Directed metric lane/track spans with source, observation date and version")
    observation_ids: list[str] = Field(default_factory=list, description="References to the shared road observation registry")
    fragments: list[dict[str, Any]] = Field(default_factory=list)
    rejected_observations: list[dict[str, Any]] = Field(default_factory=list)
    segment_id: str | None = Field(default=None, description="Стабильный ID направленного участка; формат ещё согласуется")
    from_stop_id: str | None = None
    to_stop_id: str | None = None
    path_variant_id: str | None = Field(default=None, description="Различает разные пути между той же парой остановок")
    geometry_version: str | None = None
    lateral_distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Максимальное боковое отклонение сопоставляемых геометрий на проверенном участке, в метрах; null — не вычислено")
    heading_difference_degrees: float | None = Field(default=None, ge=0, le=180, allow_inf_nan=False,
        description="Максимальная абсолютная разница локальных направлений на проверенном участке, 0..180 градусов; не условный confidence score")
    match_status: Literal["not_checked", "matched", "partial", "ambiguous", "rejected", "unavailable"]
    rejection_reasons: list[Literal["opposite_direction", "parallel_road", "insufficient_overlap", "ambiguous_geometry", "dedicated_lane", "separate_track", "stale_observation", "missing_baseline", "missing_geometry", "unsupported_mode", "provider_unavailable", "unknown"]] = Field(default_factory=list)
    lane_status: Literal["not_checked", "shared_road", "dedicated_lane", "separate_track", "unknown", "not_applicable"]
    lane_source_url: AnyUrl | None = None
    lane_source_observed_at: AwareDatetime | None = None
    coverage: TrafficCoverage | None = None
    observations: list[TrafficObservationDebug] = Field(default_factory=list)
    baseline_kind: Literal["gtfs_schedule", "otp_estimate", "distance_proportional", "unknown"] | None = None
    baseline_gtfs_version: str | None = None
    baseline_available: bool | None = Field(default=None, description="Есть ли пригодная временная база именно для этого направленного участка; null — не проверена")
    baseline_duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    corrected_duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    added_delay_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Только добавленная поправка, не доказанная чистая задержка из-за пробок")
    correction_factor: float | None = Field(default=None, ge=1, allow_inf_nan=False,
        description="Отношение скорректированного времени к явно указанной базе; не универсальный коэффициент улицы")
    applied_provider: GeoProvider | None = Field(default=None,
        description="Источник фактически применённой поправки; null — ни один не применён/не определён")
    correction_status: Literal["not_evaluated", "applied", "not_needed", "skipped", "unavailable"]
    notes: list[str] = Field(default_factory=list)


class TransitServiceDebug(Model):
    service_kind: Literal["scheduled", "frequency", "unknown"] | None = Field(default=None,
        description="Представление отправления в исходном GTFS: точное расписание либо frequencies.txt; не признак GTFS-RT")
    headway_seconds: int | None = Field(default=None, ge=1,
        description="Интервал исходного частотного обслуживания; null — неизвестен или неприменим")
    frequency_exact_times: Literal[0, 1] | None = Field(default=None,
        description="Исходный frequencies.txt exact_times: 0 — точные отправления неизвестны, 1 — интервалы задают точное расписание")
    wait_model: Literal["scheduled_departure", "half_headway", "full_headway", "otp_model", "unknown"] | None = Field(default=None,
        description="Использованная модель ожидания; модельное время не означает отправление конкретной машины")


class TransferDebug(Model):
    from_leg_index: int = Field(ge=0, description="Индекс участка до пересадки, с нуля")
    to_leg_index: int = Field(ge=0, description="Индекс следующей поездки, с нуля")
    stop_id: str | None = None
    status: Literal["not_checked", "feasible", "missed", "unknown"]
    next_service: TransitServiceDebug | None = Field(default=None, description="Временная модель следующего транспорта: частотную пересадку нельзя выдавать за проверку точного рейса")
    scheduled_departure_at: AwareDatetime | None = Field(default=None, description="Подтверждённое расписанием отправление; для exact_times=0 конкретный timestamp не выдумывается")
    corrected_arrival_at: AwareDatetime | None = None
    modeled_boarding_at: AwareDatetime | None = Field(default=None, description="Время посадки старого модельного хвоста OTP; для exact_times=0 это не точное отправление машины")
    modeled_boarding_reachable: bool | None = Field(default=None, description="Достижима ли прежняя модельная посадка после задержки; не подтверждает актуальность всего хвоста")
    tail_replanning_required: bool | None = None
    minimum_transfer_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    remaining_slack_seconds: float | None = Field(default=None, allow_inf_nan=False,
        description="Запас после минимального перехода; отрицательное значение означает нехватку времени")
    resolution: Literal["not_attempted", "kept", "kept_unverified", "next_trip", "otp_replanned", "excluded", "unresolved"]
    replacement_trip_id: str | None = None
    reason: str | None = None


class LegDebug(Model):
    service: TransitServiceDebug | None = Field(default=None, description="Исходная временная модель поездки, независимо от наличия дорожных поправок и GTFS-RT")
    traffic_status: Literal["not_evaluated", "not_applicable", "unchanged", "corrected", "partial", "unavailable"]
    original_start_at: AwareDatetime | None = None
    original_end_at: AwareDatetime | None = None
    coverage: TrafficCoverage | None = None
    segments: list[TrafficSegmentDebug] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RouteDebug(Model):
    equivalent_candidate_ids: list[str] = Field(default_factory=list,description="Original OTP IDs converging to exactly the same final journey after tail replanning")
    evaluation_group: Literal["sufficient", "incomplete", "not_applicable"] | None = Field(default=None,
        description="Группа качества оценки; непроверенный старый хвост всегда incomplete, даже при покрытии дорог >=80%")
    tail_replanning_status: Literal["not_needed", "complete", "pending", "unverified_fallback", "failed"] | None = Field(default=None,
        description="Состояние обязательного перепланирования частотного хвоста после задержки; unverified_fallback означает возврат старого достижимого хвоста с предупреждением по дедлайну")
    original_otp_route_id: str | None = None
    original_rank: int | None = Field(default=None, ge=1, description="Позиция в исходной выдаче OTP, с единицы")
    final_rank: int | None = Field(default=None, ge=1, description="Позиция после согласованного перерасчёта, с единицы")
    original_duration_seconds: int | None = Field(default=None, ge=0)
    corrected_duration_seconds: int | None = Field(default=None, ge=0)
    added_delay_seconds: int | None = Field(default=None, ge=0,description="Sum of positive road additions on retained vehicle legs; tail replanning may still reduce total journey duration")
    coverage: TrafficCoverage | None = None
    transfer_status: Literal["not_checked", "feasible", "missed", "unknown", "not_applicable"]
    transfers: list[TransferDebug] = Field(default_factory=list)
    ranking_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unverified_tail_quality(self):
        if self.tail_replanning_status == "unverified_fallback":
            if self.evaluation_group != "incomplete":
                raise ValueError("Unverified fallback tail requires incomplete evaluation group")
            if not self.warnings:
                raise ValueError("Unverified fallback tail requires an explicit warning")
        return self


class ProviderCallDebug(Model):
    waypoint_coordinates: list[tuple[float,float]] = Field(default_factory=list)
    target_edge_ids: list[str] = Field(default_factory=list)
    unrepresented_target_edge_ids: list[str] = Field(default_factory=list)
    connectors: list[LineString] = Field(default_factory=list)
    planned_chain_chord_meters: float | None = Field(default=None,ge=0)
    actual_chain_distance_meters: float | None = Field(default=None,ge=0)
    actual_chain_duration_seconds: float | None = Field(default=None,ge=0)
    useful_covered_distance_meters: float | None = Field(default=None,ge=0)
    provider: Literal["otp", "yandex", "2gis"]
    purpose: Literal["initial_candidates", "car_alternatives", "traffic_collection", "transfer_replanning"]
    status: Literal["skipped", "cache_hit", "coalesced", "succeeded", "failed", "quota_blocked"]
    skip_reason: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    elapsed_milliseconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    billable_units: int | None = Field(default=None, ge=0,
        description="Известное число тарифицируемых расчётов, не обязательно число HTTP-вызовов; null — неизвестно")
    waypoints_count: int | None = Field(default=None, ge=0)
    alternatives_received: int | None = Field(default=None, ge=0)
    error_code: str | None = None


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
    debug: LegDebug | None = Field(default=None, description="Диагностика дорожной коррекции при real traffic_mode=on. null означает, что этап не выполнялся.")
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
    debug: RouteDebug | None = Field(default=None, description="Диагностика качества, ранжирования и пересадок; null означает, что этап не выполнялся.")


class ExcludedCandidateDebug(Model):
    duplicate_of_route_id: str | None = None
    original_rank: int | None = Field(default=None,ge=1)
    reason_code: Literal["boarding_unreachable", "tail_replanning_failed", "invalid_timeline", "duplicate_after_replanning", "unknown"]
    reason: str
    original_route: Route = Field(description="Полный исходный вариант OTP до поправок, включая геометрию и legs; не является рекомендацией к поездке")
    tail_replanning_status: Literal["pending", "failed"] | None = None


class RoutesDebug(Model):
    observations: list[TrafficObservationDebug] = Field(default_factory=list)
    observation_geometries: dict[str, LineString] = Field(default_factory=dict)
    pipeline_status: Literal["not_run", "partial", "complete", "failed"]
    trace_id: str | None = Field(default=None, min_length=1,
        description="Идентификатор диагностического события без пользовательских координат и ключей API")
    algorithm_version: str | None = None
    cache_ttl_seconds: int | None = Field(default=None, ge=1, description="Согласованная свежесть 1800 секунд (30 минут) для обычного и демо-сценария; null — этап кэша не выполнялся")
    provider_calls: list[ProviderCallDebug] = Field(default_factory=list)
    candidate_count: int | None = Field(default=None, ge=0)
    returned_count: int | None = Field(default=None, ge=0)
    excluded_count: int | None = Field(default=None, ge=0)
    excluded_candidates: list[ExcludedCandidateDebug] = Field(default_factory=list,
        description="Исходные варианты, исключённые из обычной выдачи, сохраняются целиком для диагностики")
    time_budget_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Общий согласованный дедлайн 15 секунд; null — этап бюджетирования не выполнялся")
    deadline_reached: bool | None = None
    otp_replanning_count: int | None = Field(default=None, ge=0)
    baseline_gtfs_version: str | None = Field(default=None, description="Версия расписания для временной базы поправок; не подменяется последней скачанной версией")
    graph_version: str | None = None
    coverage: TrafficCoverage | None = None
    warnings: list[str] = Field(default_factory=list)



class RoutesResponse(Model):
    mode: Literal["dummy", "real"] = Field(description="dummy — демонстрационные данные; real — расчёт планировщика. Наличие пробок и realtime определяется отдельно по sources.")
    sources: RoutesResponseSources
    passenger: Passenger
    routes: list[Route]
    gtfs_version: str | None
    graph_version: str | None
    calculated_at: AwareDatetime
    warnings: list[str] = Field(description=ROUTE_WARNINGS_DESCRIPTION)
    debug: RoutesDebug | None = Field(default=None, description="Полная диагностика дорожной коррекции, возвращается всегда при real traffic_mode=on без debug-флага. Для dummy и traffic_mode=off этап не выполняется.")


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
