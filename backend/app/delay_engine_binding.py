from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

import pybind11

# Предполагается, что C++-модуль собран как `delay_engine_cpp`
delay_engine_cpp = pybind11.import_("cpp/delay_engine_cpp")


class DelayEngine:
    """Python-обёртка над C++-ядром расчёта задержек GTFS-RT."""

    def __init__(self, route_geometry: Sequence[tuple[float, float]]):
        """
        route_geometry: последовательность (lon, lat) из GeoJSON LineString.
        """
        self._geometry = [(float(lon), float(lat)) for lon, lat in route_geometry]

    def compute_trip_update(
        self,
        *,
        vehicle_lat: float,
        vehicle_lon: float,
        vehicle_timestamp: datetime,
        stop_times: list[dict],
        layover_seconds: int = 0,
    ) -> list[dict]:
        """
        stop_times: список словарей с ключами:
            - stop_id: str
            - stop_sequence: int
            - scheduled_arrival: datetime (aware)
            - scheduled_departure: datetime (aware)
            - stop_lat: float
            - stop_lon: float
        Возвращает список обновлений задержек для каждой остановки.
        """
        # 1. Map matching через C++
        match = delay_engine_cpp.map_match(
            vehicle_lat,
            vehicle_lon,
            self._geometry,
            [(st["stop_lat"], st["stop_lon"]) for st in stop_times],
        )

        # 2. Определяем текущую остановку и наблюдаемую задержку
        current_st = next(
            (st for st in stop_times if st["stop_sequence"] == match.nearest_stop_sequence),
            None,
        )
        if current_st is None:
            raise ValueError(f"Stop sequence {match.nearest_stop_sequence} not found in stop_times")

        scheduled_dep = current_st["scheduled_departure"]
        observed_delay = int((vehicle_timestamp - scheduled_dep).total_seconds())

        # 3. C++ рассчитывает распространение задержек
        cpp_stop_times = [
            delay_engine_cpp.StopTime(
                st["stop_id"],
                st["stop_sequence"],
                int(st["scheduled_arrival"].timestamp()),
                int(st["scheduled_departure"].timestamp()),
            )
            for st in stop_times
        ]
        cpp_delays = delay_engine_cpp.compute_delays(
            cpp_stop_times,
            match.nearest_stop_sequence,
            observed_delay,
            layover_seconds,
        )

        # 4. Преобразуем в Python-словари для дальнейшей валидации Pydantic
        return [
            {
                "stop_id": d.stop_id,
                "stop_sequence": d.stop_sequence,
                "arrival_delay": d.arrival_delay_sec,
                "departure_delay": d.departure_delay_sec,
            }
            for d in cpp_delays
        ]