"""Refresh the official metro reference and pMetro timings into one atomic GTFS."""

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from data.gtfs.refresh_static import CA_BUNDLE
from data.gtfs.versioned_static import atomic_json, publish, prune, read_json, store_lock
from . import official_reference as official
from .feed import WARNINGS, write_feed
from .timing import PMETRO_BASE, PMETRO_REPO, attach_timings

TABLE_NAMES = ("stations.csv", "segments.csv", "transfers.csv", "intervals.csv", "station_hours.csv")
MAX_SOURCE_BYTES = 5 * 1024 * 1024


def download(url, *, get):
    verify = str(CA_BUNDLE) if url.startswith("https://metro.spb.ru/") else True
    with get(url, timeout=(10, 30), verify=verify, stream=True,
             headers={"User-Agent": "Peresadochnaya/1.0 metro GTFS refresh"}) as response:
        response.raise_for_status()
        result = bytearray()
        for chunk in response.iter_content(65536):
            result.extend(chunk)
            if len(result) > MAX_SOURCE_BYTES:
                raise ValueError("Metro source exceeds 5 MiB")
        return bytes(result)


def fetch_sources(*, get=requests.get):
    """Fail closed if either source changes its structure; never mix snapshots."""
    pages, sources = {}, []
    for key, url in (("intervals", official.INTERVAL_URL), ("hours", official.HOURS_URL),
                     ("map", official.MAP_URL), ("graph", official.GRAPH_URL), ("coords", official.COORD_URL)):
        data = download(url, get=get)
        pages[key] = data.decode("utf-8-sig")
        sources.append({"url": url, "sha256": hashlib.sha256(data).hexdigest()})
    hours = official.extract_station_hours(pages["hours"])
    if len(hours) < 70:
        raise ValueError("Official station hours are incomplete")
    stations, segments, transfers = official.extract_network(
        pages["map"], pages["graph"], json.loads(pages["coords"]), station_hours=hours)
    official.attach_station_ids(hours, stations)
    tables = {"stations.csv": stations, "segments.csv": segments, "transfers.csv": transfers,
              "intervals.csv": official.extract_intervals(pages["intervals"]), "station_hours.csv": hours}
    commit = json.loads(download(PMETRO_REPO, get=get))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid pMetro source commit")
    url = f"{PMETRO_BASE}/{commit}/maps/Peterburg/Metro.trp"
    data = download(url, get=get)
    sources.append({"url": url, "sha256": hashlib.sha256(data).hexdigest(), "git_commit": commit})
    return attach_timings(tables, data.decode("cp1251"), url), sources


def local_sources(directory, pmetro_file):
    """Offline rebuild from Serafim's reference tables and a downloaded TRP."""
    tables, sources = {}, []
    for name in TABLE_NAMES:
        path = Path(directory) / name
        with path.open(encoding="utf-8-sig", newline="") as source:
            tables[name] = list(csv.DictReader(source))
        sources.append({"file": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    path = Path(pmetro_file)
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251")
    sources.append({"file": "Metro.trp", "sha256": hashlib.sha256(data).hexdigest()})
    return attach_timings(tables, text, f"{PMETRO_BASE}/master/maps/Peterburg/Metro.trp"), sources


def refresh(root, *, load=fetch_sources, now=None):
    root = Path(root)
    started = now or datetime.now(ZoneInfo("Europe/Moscow"))
    checked_at = started.isoformat()
    with store_lock(root):
        previous = read_json(root / "refresh-status.json")
        status = {**previous, "attempted_at": checked_at}
        atomic_json(root / "refresh-status.json", {**status, "status": "running"})
        try:
            reference, sources = load()
            completed = now or datetime.now(ZoneInfo("Europe/Moscow"))
            checked_at = completed.isoformat()
            with tempfile.TemporaryDirectory(prefix=".metro-", dir=root) as directory:
                incoming = Path(directory) / "feed.zip"
                counts = write_feed(incoming, reference, completed.date(), sources)
                # All parsing and feed construction completed before switching current.json.
                result = publish(root, incoming, completed.strftime("%Y%m%d"), checked_at)
            status.update(result)
            status.update(counts, last_success_at=checked_at, error=None, warnings=WARNINGS)
            atomic_json(root / "refresh-status.json", status)
            prune(root)
            return status
        except Exception as exc:
            # Requests exceptions can include a proxy URL; credentials never enter logs/status.
            atomic_json(root / "refresh-status.json", {**status, "status": "error", "error": type(exc).__name__})
            raise


def main():
    parser = argparse.ArgumentParser(description="Обновить оценочный GTFS метро Петербурга")
    parser.add_argument("--store-dir", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, help="Локальные официальные CSV вместо сетевого обновления")
    parser.add_argument("--pmetro-file", type=Path, help="Локальный Metro.trp для пересборки без сети")
    args = parser.parse_args()
    if bool(args.reference_dir) != bool(args.pmetro_file):
        parser.error("--reference-dir и --pmetro-file задаются вместе")
    load = (lambda: local_sources(args.reference_dir, args.pmetro_file)) if args.reference_dir else fetch_sources
    try:
        print(json.dumps(refresh(args.store_dir, load=load), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, requests.RequestException) as exc:
        print(f"Ошибка обновления GTFS метро: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
