# python/trip_updates_schemas.py
"""Внутренние схемы TripUpdate для GTFS-RT; не являются protobuf-сообщениями."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from pydantic import BaseModel, ConfigDict

class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


ScheduleRelationship = Literal["SCHEDULED", "SKIPPED", "NO_DATA", "UNSCHEDULED"]

TripScheduleRelationship = Literal[
    "SCHEDULED",
    "ADDED",
    "UNSCHEDULED",
    "CANCELED",
    "DUPLICATED",
    "DELETED",
]


class TripDescriptor(Model):
    trip_id: str | None = None
    route_id: str | None = None
    direction_id: Literal[0, 1] | None = None
    start_time: str | None = Field(
        default=None,
        pattern=r"^\d{2}:[0-5]\d:[0-5]\d$",
        description="Время начала рейса по расписанию, HH:MM:SS; может быть > 24:00:00",
    )
    start_date: date | None = None
    schedule_relationship: TripScheduleRelationship = "SCHEDULED"


class StopTimeEvent(Model):
    delay: int | None = Field(
        default=None,
        description="Задержка в секундах; положительная — отставание, отрицательная — опережение",
    )
    time: AwareDatetime | None = Field(
        default=None,
        description="Абсолютное прогнозируемое время прибытия/отправления",
    )
    uncertainty: int | None = Field(
        default=None,
        ge=0,
        description="Оценка неопределённости прогноза в секундах",
    )

    @model_validator(mode="after")
    def at_least_one_of_delay_or_time(self):
        if self.delay is None and self.time is None:
            raise ValueError("StopTimeEvent должен содержать хотя бы одно из полей: delay или time")
        return self


class StopTimeUpdate(Model):
    stop_sequence: int | None = Field(default=None, ge=0)
    stop_id: str | None = None
    arrival: StopTimeEvent | None = None
    departure: StopTimeEvent | None = None
    schedule_relationship: ScheduleRelationship = "SCHEDULED"

    @model_validator(mode="after")
    def at_least_one_stop_identifier(self):
        if self.stop_sequence is None and self.stop_id is None:
            raise ValueError("StopTimeUpdate должен содержать stop_sequence или stop_id")
        return self


class VehicleDescriptor(Model):
    id: str | None = None
    label: str | None = None
    license_plate: str | None = None


class TripUpdate(Model):
    trip: TripDescriptor
    vehicle: VehicleDescriptor | None = None
    stop_time_update: list[StopTimeUpdate] = Field(default_factory=list)
    timestamp: AwareDatetime | None = None
    delay: int | None = Field(
        default=None,
        description="Текущая задержка рейса; должна отражать наблюдаемое отклонение от расписания",
    )

    @model_validator(mode="after")
    def stop_time_updates_ordered(self):
        seqs = [u.stop_sequence for u in self.stop_time_update if u.stop_sequence is not None]
        if seqs != sorted(seqs):
            raise ValueError("stop_time_update должны быть упорядочены по возрастанию stop_sequence")
        if len(seqs) != len(set(seqs)):
            raise ValueError("stop_sequence не должны повторяться")
        return self
    