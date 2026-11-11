"""Build an explicitly approximate, frequency-based metro GTFS for OTP."""

import csv
import io
import json
import zipfile
from datetime import timedelta

from .timing import name_key

WARNINGS = [
    "Approximate community running/transfer times from pMetro; not an official timetable.",
    "Headway is the rounded midpoint of the published range; exact_times=0.",
    "Line-wide headways apply in both directions; depot trips and special holiday schedules are unknown.",
    "Service is limited to published headway windows and terminal first/last train bounds; coverage may be incomplete.",
    "Running times are symmetric and include no additional dwell allowance; station access times are not modelled.",
]
DAY_FLAGS = {"weekday": (1, 1, 1, 1, 1, 0, 0), "saturday": (0, 0, 0, 0, 0, 1, 0),
             "sunday": (0, 0, 0, 0, 0, 0, 1)}
COLORS = {1: "D6083B", 2: "0078C9", 3: "009A49", 4: "EA7125", 5: "702785", 6: "A86B32"}


def clock(value):
    hour, minute = map(int, value.split(":"))
    if not 0 <= hour <= 47 or not 0 <= minute < 60:
        raise ValueError(f"Invalid clock time: {value}")
    return (hour + (24 if hour < 4 else 0)) * 3600 + minute * 60


def gtfs_time(value):
    return f"{value // 3600:02}:{value // 60 % 60:02}:{value % 60:02}"


def terminal_window(hours, origin, destination):
    first, last = [], []
    for row in hours:
        if row["station_id"] != origin["station_id"] or row["status"]:
            continue
        for side in ("first", "second"):
            if name_key(row[f"{side}_terminus"]) != name_key(destination["name"]):
                continue
            first.extend(clock(row[key]) for key in (
                f"first_to_{side}_terminus_odd", f"first_to_{side}_terminus_even") if row[key])
            if row[f"last_to_{side}_terminus"]:
                last.append(clock(row[f"last_to_{side}_terminus"]))
    if not first or not last or max(first) >= min(last):
        raise ValueError(f"Missing/invalid terminal hours at {origin['name']}")
    return max(first), min(last)


def build_tables(reference, service_date):
    stations = reference["stations.csv"]
    if len({s["station_id"] for s in stations}) != len(stations):
        raise ValueError("Duplicate station IDs")
    stops = [{"stop_id": s["station_id"], "stop_name": s["name"], "stop_lat": s["lat"],
              "stop_lon": s["lon"], "location_type": 0} for s in stations]
    routes, trips, stop_times, frequencies, calendar = [], [], [], [], []
    edges = {frozenset((s["from_station_id"], s["to_station_id"])): int(s["duration_seconds"])
             for s in reference["segments.csv"]}
    for line in sorted({int(s["line"]) for s in stations}):
        ordered = sorted((s for s in stations if int(s["line"]) == line), key=lambda s: int(s["sequence"]))
        if len(ordered) < 2:
            raise ValueError(f"Line {line} has fewer than two stations")
        routes.append({"route_id": f"metro:{line}", "agency_id": "spb-metro",
                       "route_short_name": str(line), "route_long_name": f"{ordered[0]['name']} — {ordered[-1]['name']}",
                       "route_type": 1, "route_color": COLORS.get(line, "555555"), "route_text_color": "FFFFFF"})
        for direction in (0, 1):
            sequence = ordered if direction == 0 else ordered[::-1]
            first, last = terminal_window(reference["station_hours.csv"], sequence[0], sequence[-1])
            for day, flags in DAY_FLAGS.items():
                sid = f"metro:{day}"
                tid = f"metro:{line}:{direction}:{day}"
                windows = []
                for row in reference["intervals.csv"]:
                    if int(row["line"]) != line or row["day_type"] != day:
                        continue
                    minimum, maximum = int(row["min_headway_seconds"]), int(row["max_headway_seconds"])
                    if not 0 < minimum <= maximum:
                        raise ValueError("Invalid headway range")
                    begin, end = clock(row["period_start"]), clock(row["period_end"])
                    if end <= begin:
                        raise ValueError("Invalid headway window")
                    begin, end = max(begin, first), min(end, last)
                    if begin < end:
                        windows.append((begin, end, (minimum + maximum + 1) // 2))
                windows.sort()
                if not windows or any(a[1] > b[0] for a, b in zip(windows, windows[1:])):
                    raise ValueError(f"Missing/overlapping headway windows for {tid}")
                trips.append({"route_id": f"metro:{line}", "service_id": sid, "trip_id": tid,
                              "trip_headsign": sequence[-1]["name"], "direction_id": direction})
                elapsed = 0
                for i, station in enumerate(sequence):
                    if i:
                        duration = edges.get(frozenset((sequence[i - 1]["station_id"], station["station_id"])))
                        if duration is None or duration <= 0:
                            raise ValueError("Missing positive running time")
                        elapsed += duration
                    closed = int(station["boarding_allowed"]) == 0
                    stop_times.append({"trip_id": tid, "arrival_time": gtfs_time(elapsed),
                                       "departure_time": gtfs_time(elapsed), "stop_id": station["station_id"],
                                       "stop_sequence": i + 1, "pickup_type": int(closed),
                                       "drop_off_type": int(closed), "timepoint": 0})
                frequencies.extend({"trip_id": tid, "start_time": gtfs_time(a), "end_time": gtfs_time(b),
                                    "headway_secs": h, "exact_times": 0} for a, b, h in windows)
    valid_from = max(str(row["source_valid_from"]) for name in ("intervals.csv", "station_hours.csv")
                     for row in reference[name])
    start = max(service_date.strftime("%Y%m%d"), valid_from)
    end = (service_date + timedelta(days=30)).strftime("%Y%m%d")
    if start > end:
        raise ValueError("Official source starts beyond feed validity")
    for day, flags in DAY_FLAGS.items():
        calendar.append({"service_id": f"metro:{day}", **dict(zip(
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), flags)),
                         "start_date": start, "end_date": end})
    index = {s["station_id"]: s for s in stations}
    transfers = []
    for row in reference["transfers.csv"]:
        a, b = row["station_a_id"], row["station_b_id"]
        if not int(index[a]["boarding_allowed"]) or not int(index[b]["boarding_allowed"]):
            continue
        for origin, destination in ((a, b), (b, a)):
            transfers.append({"from_stop_id": origin, "to_stop_id": destination,
                              "transfer_type": 2, "min_transfer_time": int(row["duration_seconds"])})
    return {
        "agency.txt": [{"agency_id": "spb-metro", "agency_name": "Петербургский метрополитен (оценочный GTFS)",
                        "agency_url": "https://metro.spb.ru", "agency_timezone": "Europe/Moscow", "agency_lang": "ru"}],
        "stops.txt": stops, "routes.txt": routes, "trips.txt": trips, "stop_times.txt": stop_times,
        "frequencies.txt": frequencies, "calendar.txt": calendar,
        "calendar_dates.txt": [], "transfers.txt": transfers,
        "feed_info.txt": [{"feed_publisher_name": "Peresadochnaya", "feed_publisher_url": "https://github.com/Peresadochnaya/peresadochnaya",
                           "feed_lang": "ru", "feed_start_date": start, "feed_end_date": end}],
    }


def write_feed(path, reference, service_date, provenance):
    tables = build_tables(reference, service_date)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rows in tables.items():
            stream = io.StringIO(newline="")
            fields = list(rows[0]) if rows else ["service_id", "date", "exception_type"]
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, stream.getvalue().encode("utf-8"))
        member = zipfile.ZipInfo("metro_reference.json", date_time=(1980, 1, 1, 0, 0, 0))
        member.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(member, json.dumps({"warnings": WARNINGS, "sources": provenance,
                                           "tables": reference}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {"metro_routes": len(tables["routes.txt"]), "metro_stations": len(tables["stops.txt"]),
            "metro_segments": len(reference["segments.csv"]), "metro_transfers": len(reference["transfers.csv"])}
