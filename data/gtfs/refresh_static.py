"""Refresh the SPb GTFS ZIP and CSV files used by the backend."""

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

URL = "https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs/feed.zip"
DATA_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ARCHIVE = DATA_DIR / "raw" / "portal-gtfs.zip"
DEFAULT_CSV_DIR = DATA_DIR / "processed" / "gtfs"
CA_BUNDLE = Path(__file__).resolve().parent / "certs" / "spb_gtfs_ca_bundle.pem"
REQUIRED = {"agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt", "calendar.txt",
            "calendar_dates.txt"}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_archive(path, today):
    """Check ZIP integrity and report whether it schedules service today."""
    try:
        with zipfile.ZipFile(path) as archive:
            missing = REQUIRED - set(archive.namelist())
            if missing:
                raise ValueError(f"В GTFS ZIP нет файлов: {', '.join(sorted(missing))}")
            damaged = archive.testzip()
            if damaged:
                raise ValueError(f"Повреждён файл внутри ZIP: {damaged}")

            def rows(filename):
                with archive.open(filename) as raw:
                    yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace"))

            routes = sum(1 for _ in rows("routes.txt"))
            stops = sum(1 for _ in rows("stops.txt"))
            weekday = datetime.strptime(today, "%Y%m%d").strftime("%A").lower()
            active = set()
            invalid = 0
            for row in rows("calendar.txt"):
                if row["start_date"] > row["end_date"]:
                    invalid += 1
                if row["start_date"] <= today <= row["end_date"] and row[weekday] == "1":
                    active.add(row["service_id"])

            latest_exception = None
            for row in rows("calendar_dates.txt"):
                date = row["date"]
                latest_exception = max(latest_exception or date, date)
                if date == today:
                    if row["exception_type"] == "1":
                        active.add(row["service_id"])
                    elif row["exception_type"] == "2":
                        active.discard(row["service_id"])
    except zipfile.BadZipFile as exc:
        raise ValueError("Ответ сервера не является корректным GTFS ZIP") from exc

    return {
        "routes": routes,
        "stops": stops,
        "invalid_calendar_rows": invalid,
        "active_service_ids": len(active),
        "latest_exception_date": latest_exception,
        "service_date": today,
        "warning": "Нет действующих service_id на указанную дату" if not active else None,
    }


def prepare_csv(archive_path, csv_dir, source_hash=None):
    """Copy GTFS CSV tables from ZIP to a replaceable directory."""
    archive_path = Path(archive_path)
    csv_dir = Path(csv_dir)
    source_hash = source_hash or sha256(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        tables = {}
        for member in archive.infolist():
            if not member.filename.endswith(".txt"):
                continue
            if Path(member.filename).name != member.filename:
                raise ValueError(f"GTFS таблица должна лежать в корне ZIP: {member.filename}")
            csv_name = member.filename[:-4] + ".csv"
            if csv_name in tables:
                raise ValueError(f"Повторяется GTFS таблица: {member.filename}")
            tables[csv_name] = member

        expected = {name: member.file_size for name, member in tables.items()}
        manifest_path = csv_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
        if (manifest == {"source_sha256": source_hash, "files": expected}
                and all((csv_dir / name).is_file()
                        and (csv_dir / name).stat().st_size == size
                        for name, size in expected.items())):
            return {"csv_status": "unchanged", "csv_dir": str(csv_dir), "csv_files": len(tables)}

        csv_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".gtfs-csv-", dir=csv_dir.parent))
        previous = None
        try:
            for name, member in tables.items():
                with archive.open(member) as source, (staging / name).open("wb") as target:
                    shutil.copyfileobj(source, target)
                if (staging / name).stat().st_size != member.file_size:
                    raise ValueError(f"Не полностью извлечена таблица: {member.filename}")
            (staging / "manifest.json").write_text(
                json.dumps({"source_sha256": source_hash, "files": expected}, ensure_ascii=False),
                encoding="utf-8",
            )
            if csv_dir.exists():
                if not csv_dir.is_dir():
                    raise ValueError(f"Путь для CSV не является папкой: {csv_dir}")
                previous = Path(tempfile.mkdtemp(prefix=".gtfs-csv-old-", dir=csv_dir.parent))
                previous.rmdir()
                os.replace(csv_dir, previous)
            try:
                os.replace(staging, csv_dir)
            except OSError:
                if previous is not None:
                    os.replace(previous, csv_dir)
                    previous = None
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if previous is not None and previous.exists():
                shutil.rmtree(previous, ignore_errors=True)
    return {"csv_status": "updated", "csv_dir": str(csv_dir), "csv_files": len(tables)}


def activate_archive(incoming, current, today, csv_dir=None):
    """Validate and activate ZIP, then refresh matching CSV files."""
    incoming = Path(incoming)
    current = Path(current)
    details = inspect_archive(incoming, today)
    new_hash = sha256(incoming)
    old_hash = sha256(current) if current.exists() else None
    if new_hash == old_hash:
        status = "unchanged"
    else:
        if current.exists():
            backup = current.with_name(current.stem + ".previous.zip")
            shutil.copy2(current, backup)
        os.replace(incoming, current)
        status = "updated"
    try:
        csv_result = prepare_csv(current, csv_dir, new_hash) if csv_dir is not None else {}
    except (OSError, ValueError, zipfile.BadZipFile):
        if status == "updated":
            if old_hash is None:
                current.unlink(missing_ok=True)
            else:
                shutil.copy2(current.with_name(current.stem + ".previous.zip"), current)
        raise
    return {"status": status, "sha256": new_hash, "previous_sha256": old_hash,
            **details, **csv_result}


def main():
    parser = argparse.ArgumentParser(description="Ежедневно обновить GTFS Static Санкт-Петербурга")
    parser.add_argument("--output", type=Path, default=DEFAULT_ARCHIVE, help="путь к текущему ZIP")
    parser.add_argument("--csv-dir", type=Path, default=DEFAULT_CSV_DIR, help="папка для извлечённых CSV")
    parser.add_argument("--ca-bundle", type=Path, default=CA_BUNDLE, help="доверенные CA-сертификаты")
    args = parser.parse_args()
    if not args.ca_bundle.is_file():
        parser.error(f"CA-файл не найден: {args.ca_bundle}")

    today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=args.output.parent, suffix=".zip", delete=False) as target:
            temporary = Path(target.name)
            with requests.get(URL, timeout=(10, 120), stream=True, verify=str(args.ca_bundle)) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        target.write(chunk)
        result = activate_archive(temporary, args.output, today, csv_dir=args.csv_dir)
        result["checked_at"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()
        result["archive"] = str(args.output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"Ошибка обновления GTFS: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
