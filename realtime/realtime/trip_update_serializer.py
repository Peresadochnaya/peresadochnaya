# python/trip_update_serializer.py
"""Сериализация списка TripUpdate в бинарный FeedMessage GTFS-RT.

Требует пакет gtfs-realtime-bindings:
    pip install gtfs-realtime-bindings
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from google.protobuf.json_format import MessageToDict
from google.transit import gtfs_realtime_pb2

from .trip_updates_schemas import TripUpdate

# ---------- Таблицы соответствия enum'ов схемы и protobuf ----------

_TRIP_REL_MAP = {
    "SCHEDULED":   gtfs_realtime_pb2.TripDescriptor.SCHEDULED,
    "ADDED":       gtfs_realtime_pb2.TripDescriptor.ADDED,
    "UNSCHEDULED": gtfs_realtime_pb2.TripDescriptor.UNSCHEDULED,
    "CANCELED":    gtfs_realtime_pb2.TripDescriptor.CANCELED,
    "DUPLICATED":  gtfs_realtime_pb2.TripDescriptor.DUPLICATED,
    "DELETED":     gtfs_realtime_pb2.TripDescriptor.DELETED,
}

_STOP_REL_MAP = {
    "SCHEDULED":   gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SCHEDULED,
    "SKIPPED":     gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED,
    "NO_DATA":     gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.NO_DATA,
    "UNSCHEDULED": gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.UNSCHEDULED,
}


# ---------- Публичный API ----------

def serialize_feed(
    trip_updates: Iterable[TripUpdate],
    *,
    feed_timestamp: datetime,
    feed_version: str = "2.0",
    incrementality: str = "FULL_DATASET",
) -> bytes:
    """Сериализует набор TripUpdate в бинарный FeedMessage."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = feed_version
    feed.header.incrementality = (
        gtfs_realtime_pb2.FeedHeader.FULL_DATASET
        if incrementality == "FULL_DATASET"
        else gtfs_realtime_pb2.FeedHeader.DIFFERENTIAL
    )
    feed.header.timestamp = int(feed_timestamp.timestamp())

    seen_entity_ids: set[str] = set()
    for trip_update in trip_updates:
        entity = feed.entity.add()
        base_id = _entity_id(trip_update)
        entity_id, counter = base_id, 0
        while entity_id in seen_entity_ids:
            counter += 1
            entity_id = f"{base_id}_{counter}"
        seen_entity_ids.add(entity_id)
        entity.id = entity_id
        _fill_trip_update(entity.trip_update, trip_update)

    return feed.SerializeToString()


def deserialize_feed(binary: bytes) -> gtfs_realtime_pb2.FeedMessage:
    """Разбирает сериализованный FeedMessage; удобно в тестах."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(binary)
    return feed


def feed_to_dict(binary: bytes) -> dict:
    """Человекочитаемое представление для логов и отладки."""
    return MessageToDict(deserialize_feed(binary))


# ---------- Внутренние помощники ----------

def _entity_id(trip_update: TripUpdate) -> str:
    """Стабильный id сущности, чтобы FULL_DATASET-фид корректно обновлялся."""
    parts = ["trip_update"]
    if trip_update.trip.trip_id:
        parts.append(trip_update.trip.trip_id)
    if trip_update.vehicle and trip_update.vehicle.id:
        parts.append(trip_update.vehicle.id)
    return "_".join(parts)


def _fill_trip_update(
    proto: gtfs_realtime_pb2.TripUpdate,
    src: TripUpdate,
) -> None:
    # --- TripDescriptor ---
    if src.trip.trip_id:
        proto.trip.trip_id = src.trip.trip_id
    if src.trip.route_id:
        proto.trip.route_id = src.trip.route_id
    if src.trip.direction_id is not None:
        proto.trip.direction_id = src.trip.direction_id
    if src.trip.start_time:
        proto.trip.start_time = src.trip.start_time
    if src.trip.start_date:
        proto.trip.start_date = src.trip.start_date.strftime("%Y%m%d")
    proto.trip.schedule_relationship = _TRIP_REL_MAP[src.trip.schedule_relationship]

    # --- VehicleDescriptor ---
    if src.vehicle is not None:
        if src.vehicle.id:
            proto.vehicle.id = src.vehicle.id
        if src.vehicle.label:
            proto.vehicle.label = src.vehicle.label
        if src.vehicle.license_plate:
            proto.vehicle.license_plate = src.vehicle.license_plate

    # --- Timestamp и trip-level delay ---
    if src.timestamp is not None:
        proto.timestamp = int(src.timestamp.timestamp())
    if src.delay is not None:
        proto.delay = src.delay

    # --- StopTimeUpdate ---
    for stu in src.stop_time_update:
        proto_stu = proto.stop_time_update.add()
        if stu.stop_sequence is not None:
            proto_stu.stop_sequence = stu.stop_sequence
        if stu.stop_id:
            proto_stu.stop_id = stu.stop_id
        proto_stu.schedule_relationship = _STOP_REL_MAP[stu.schedule_relationship]
        if stu.arrival is not None:
            _fill_stop_event(proto_stu.arrival, stu.arrival)
        if stu.departure is not None:
            _fill_stop_event(proto_stu.departure, stu.departure)


def _fill_stop_event(proto, src) -> None:
    if src.delay is not None:
        proto.delay = src.delay
    if src.time is not None:
        proto.time = int(src.time.timestamp())
    if src.uncertainty is not None:
        proto.uncertainty = src.uncertainty