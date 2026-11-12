# python/trip_update_builder.py
"""Преобразование выхода DelayEngine и контекста рейса в схему TripUpdate.

Ответственность модуля — только сборка и валидация внутренней модели.
Сериализация в protobuf живёт в trip_update_serializer.py.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

from .trip_updates_schemas import (
    StopTimeEvent,
    StopTimeUpdate,
    TripDescriptor,
    TripUpdate,
    VehicleDescriptor,
)

# Явные словари типов, чтобы mypy не ругался на Literal из Pydantic
_STOP_REL_SCHEDULED = "SCHEDULED"
_STOP_REL_SKIPPED = "SKIPPED"


def build_trip_update(
    *,
    trip_id: str,
    route_id: str,
    direction_id: int | None,
    start_time: str | None,
    start_date: date | None,
    vehicle_id: str,
    vehicle_label: str | None,
    vehicle_timestamp: datetime,
    current_stop_sequence: int,
    engine_delays: Sequence[dict],
    include_trip_level_delay: bool = False,
    include_past_stops: bool = False,
    include_first_arrival: bool = False,
    include_last_departure: bool = False,
) -> TripUpdate:
    """Собирает TripUpdate из результата DelayEngine.compute_trip_update.

    engine_delays — список словарей вида
        {"stop_id": str, "stop_sequence": int,
         "arrival_delay": int, "departure_delay": int}

    current_stop_sequence — stop_sequence ближайшей к ТС остановки
        (обычно берётся из MapMatchResult.nearest_stop_sequence).

    include_trip_level_delay — по GTFS-RT Best Practices trip.delay НЕ следует
        заполнять, если есть stop_time_update. По умолчанию False.

    include_past_stops — прошедшие остановки по умолчанию опускаются.

    include_first_arrival / include_last_departure — управляют краевыми
        событиями первой и последней остановки рейса.
    """
    delays_sorted = sorted(engine_delays, key=lambda d: d["stop_sequence"])
    if not delays_sorted:
        raise ValueError("engine_delays is empty")

    seqs = [d["stop_sequence"] for d in delays_sorted]
    if len(seqs) != len(set(seqs)):
        raise ValueError("engine_delays contains duplicate stop_sequence")

    min_seq = seqs[0]
    max_seq = seqs[-1]

    stop_time_updates: list[StopTimeUpdate] = []

    for d in delays_sorted:
        seq = d["stop_sequence"]
        if not include_past_stops and seq < current_stop_sequence:
            continue

        is_first = seq == min_seq
        is_last = seq == max_seq

        arrival_event: StopTimeEvent | None = None
        departure_event: StopTimeEvent | None = None

        if d.get("arrival_delay") is not None and not (
            is_first and not include_first_arrival
        ):
            arrival_event = StopTimeEvent(delay=int(d["arrival_delay"]))

        if d.get("departure_delay") is not None and not (
            is_last and not include_last_departure
        ):
            departure_event = StopTimeEvent(delay=int(d["departure_delay"]))

        # SCHEDULED без arrival и departure бессмысленно — пропускаем
        if arrival_event is None and departure_event is None:
            continue

        stop_time_updates.append(
            StopTimeUpdate(
                stop_sequence=seq,
                stop_id=d["stop_id"],
                arrival=arrival_event,
                departure=departure_event,
                schedule_relationship=_STOP_REL_SCHEDULED,
            )
        )

    if not stop_time_updates:
        raise ValueError("No stop_time_updates survived filtering")

    trip_delay: int | None = None
    if include_trip_level_delay:
        current = next(
            (d for d in delays_sorted if d["stop_sequence"] == current_stop_sequence),
            None,
        )
        if current is not None:
            raw = current.get("departure_delay")
            if raw is None:
                raw = current.get("arrival_delay")
            if raw is not None:
                trip_delay = int(raw)

    return TripUpdate(
        trip=TripDescriptor(
            trip_id=trip_id,
            route_id=route_id,
            direction_id=direction_id,
            start_time=start_time,
            start_date=start_date,
            schedule_relationship="SCHEDULED",
        ),
        vehicle=VehicleDescriptor(id=vehicle_id, label=vehicle_label),
        stop_time_update=stop_time_updates,
        timestamp=vehicle_timestamp,
        delay=trip_delay,
    )


def build_trip_update_for_vehicle(
    *,
    vehicle_position,                 # VehiclePosition из vehicles_schemas.py
    gtfs_trip,                        # GtfsTrip из gtfs_schemas.py
    current_stop_sequence: int,
    engine_delays: Sequence[dict],
    service_date: date,
    vehicle_label: str | None = None,
) -> TripUpdate:
    """Удобная обёртка для вызова прямо из вашего пайплайна.

    Позволяет не разворачивать поля trip/vehicle руками.
    """
    if vehicle_position.trip_id is None:
        raise ValueError("VehiclePosition.trip_id is required to build TripUpdate")
    if not gtfs_trip.stop_times:
        raise ValueError("GtfsTrip has no stop_times")

    start_time = _trip_start_time(gtfs_trip)
    # Для ночных рейсов после полуночи service_date может отличаться от даты
    # timestamp — при необходимости переопределите это снаружи.
    start_date = service_date

    return build_trip_update(
        trip_id=vehicle_position.trip_id,
        route_id=gtfs_trip.route_id,
        direction_id=gtfs_trip.direction_id,
        start_time=start_time,
        start_date=start_date,
        vehicle_id=vehicle_position.vehicle_id,
        vehicle_label=vehicle_label or vehicle_position.label,
        vehicle_timestamp=vehicle_position.timestamp,
        current_stop_sequence=current_stop_sequence,
        engine_delays=engine_delays,
    )


def _trip_start_time(gtfs_trip) -> str | None:
    """departure_time первой остановки как TripDescriptor.start_time."""
    first = min(gtfs_trip.stop_times, key=lambda s: s.stop_sequence)
    return first.departure_time or first.arrival_time
