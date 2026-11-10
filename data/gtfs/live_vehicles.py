"""Fetch fresh GTFS-RT vehicle positions and show one route."""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

URL = "https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs/realtime/vehicle"
MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_CA_BUNDLE = Path(__file__).resolve().parent / "certs" / "spb_gtfs_ca_bundle.pem"


def moscow_time(timestamp):
    return datetime.fromtimestamp(timestamp, MOSCOW).strftime("%H:%M:%S")


def build_snapshot(payload, route_id=None, fetched_at=None):
    """Turn one GTFS-RT response into JSON-serializable vehicle data."""
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(payload)
    except DecodeError as exc:
        raise ValueError("Ответ сервера не является GTFS-RT protobuf") from exc

    now = fetched_at if fetched_at is not None else time.time()
    header_time = int(feed.header.timestamp) if feed.header.HasField("timestamp") else None
    vehicles = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        if route_id is not None and vehicle.trip.route_id != route_id:
            continue
        if not vehicle.HasField("position"):
            continue
        measured_at = int(vehicle.timestamp) if vehicle.HasField("timestamp") else None
        age = round(now - measured_at) if measured_at is not None else None
        position = vehicle.position
        vehicles.append({
            "vehicle_id": vehicle.vehicle.id or entity.id,
            "label": vehicle.vehicle.label or None,
            "route_id": vehicle.trip.route_id or None,
            "trip_id": vehicle.trip.trip_id or None,
            "stop_id": vehicle.stop_id or None,
            "latitude": float(position.latitude),
            "longitude": float(position.longitude),
            "bearing_deg": float(position.bearing) if position.HasField("bearing") else None,
            "speed_mps": float(position.speed) if position.HasField("speed") else None,
            "observed_at": measured_at,
            "age_seconds": age,
            "is_stale": age is None or age > 90 or age < -30,
        })

    feed_age = round(now - header_time) if header_time is not None else None
    return {
        "source_url": URL,
        "gtfs_realtime_version": feed.header.gtfs_realtime_version,
        "fetched_at": round(now),
        "feed_timestamp": header_time,
        "feed_age_seconds": feed_age,
        "feed_is_stale": feed_age is None or feed_age > 90 or feed_age < -30,
        "route_filter": route_id,
        "vehicles": vehicles,
    }


def fetch_snapshot(route_id=None, verify=None, timeout=20):
    """Fetch a new snapshot with TLS verification enabled by default."""
    ca_setting = str(DEFAULT_CA_BUNDLE) if verify is None else verify
    response = requests.get(URL, timeout=timeout, verify=ca_setting)
    response.raise_for_status()
    if "protobuf" not in response.headers.get("Content-Type", "").lower():
        raise ValueError(f"Неожиданный Content-Type: {response.headers.get('Content-Type')}")
    return build_snapshot(response.content, route_id)


def show_positions(snapshot):
    header_time = snapshot["feed_timestamp"]
    header_age = snapshot["feed_age_seconds"]
    vehicles = snapshot["vehicles"]
    route_id = snapshot["route_filter"] or "все"

    print(
        f"Снимок: {moscow_time(header_time) if header_time else 'нет времени'}, "
        f"возраст: {header_age if header_age is not None else '?'} с; "
        f"маршрут {route_id}: {len(vehicles)} машин"
    )
    if snapshot["feed_is_stale"]:
        print("Внимание: время формирования потока отсутствует или старше 90 секунд")

    for vehicle in vehicles:
        measured_at = vehicle["observed_at"]
        age = vehicle["age_seconds"]
        marker = "УСТАРЕЛО" if vehicle["is_stale"] else ""
        speed = vehicle["speed_mps"]
        speed_text = f"{speed * 3.6:.1f}" if speed is not None else "?"
        print(
            f"{vehicle['label'] or vehicle['vehicle_id']:>6} "
            f"{vehicle['latitude']:.6f}, {vehicle['longitude']:.6f} "
            f"{speed_text} км/ч "
            f"{moscow_time(measured_at) if measured_at else 'нет времени'} "
            f"({age if age is not None else '?'} с) {marker}"
        )


def main():
    parser = argparse.ArgumentParser(description="Свежие положения транспорта из GTFS-RT СПб")
    parser.add_argument("--route-id", default="1087", help="GTFS route_id; 1087 — троллейбус №31")
    parser.add_argument("--all", action="store_true", help="все маршруты, без фильтра")
    parser.add_argument("--json", action="store_true", help="один JSON-объект на запрос для backend")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="повторять запрос каждые N секунд, минимум 30")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--ca-bundle", metavar="PATH", help="файл с доверенным корневым сертификатом")
    tls.add_argument("--insecure", action="store_true", help="отключить проверку TLS только для диагностики")
    args = parser.parse_args()
    if args.watch is not None and args.watch < 30:
        parser.error("--watch должен быть не меньше 30 секунд")

    verify = False if args.insecure else (args.ca_bundle or str(DEFAULT_CA_BUNDLE))
    if not args.insecure and not Path(verify).is_file():
        parser.error(f"CA-файл не найден: {verify}")
    if args.insecure:
        from urllib3 import disable_warnings
        from urllib3.exceptions import InsecureRequestWarning

        disable_warnings(InsecureRequestWarning)
        print("Внимание: TLS-сертификат не проверяется; только для диагностики", file=sys.stderr)

    while True:
        try:
            snapshot = fetch_snapshot(None if args.all else args.route_id, verify=verify)
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=False), flush=True)
            else:
                show_positions(snapshot)
        except (requests.RequestException, ValueError) as exc:
            print(f"Ошибка получения GTFS-RT: {exc}", file=sys.stderr)
            if args.watch is None:
                return 1
        if args.watch is None:
            return 0
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nОстановлено")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
