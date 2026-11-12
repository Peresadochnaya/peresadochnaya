#!/usr/bin/env python3
"""Версионное хранилище графа OTP: подготовка входных данных и публикация.

Зачем это нужно
---------------
OTP загружает ``graph.obj`` из базового каталога и не умеет выбирать версию.
Поэтому версия графа — это отдельный самодостаточный каталог с ``graph.obj``
и тремя JSON-конфигами, а указатель ``current`` и ``current.json`` публикуются
атомарно. Backend читает ``current.json`` и не зависит от истории версий.

Раскладка каталога данных (по умолчанию ``otp/data``)::

    osm/spb-lo.osm.pbf          канонический OSM, обновляется вручную
    staging/                    временный каталог сборки (входы + graph.obj)
    versions/<version>/         опубликованный граф: graph.obj, manifest.json,
                                otp-config.json, build-config.json,
                                router-config.json
    current -> versions/<version>
    current.json                самодостаточные метаданные активной версии
    build-status.json           результат последней попытки сборки
    .build.lock                 неблокирующая блокировка prepare/publish

Команды
-------
    graph_store.py prepare  --data-dir otp/data [--gtfs-zip FILE | --gtfs-store DIR]
                            [--metro-store DIR | --metro-zip FILE | --no-metro]
    graph_store.py publish  --data-dir otp/data
    graph_store.py verify   --data-dir otp/data --url http://localhost:8080
    graph_store.py status   --data-dir otp/data

``prepare`` копирует GTFS в staging, исправляет календарь копии и вычисляет
версию графа. Исходный ZIP не изменяется. ``publish`` переносит staging в
``versions/<version>`` и переключает указатель только после успешной сборки.

В граф всегда попадают два фида: наземный (``feed.gtfs.zip``) и оценочный
GTFS метро (``metro.gtfs.zip``) из ``data/metro``. Метро можно отключить только
явным ``--no-metro``: пропавший фид — ошибка, а не тихая сборка без подземки.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
OTP_DIR = SCRIPT_DIR.parent
ROOT_DIR = OTP_DIR.parent
DEFAULT_DATA_DIR = OTP_DIR / "data"
DEFAULT_CONFIG_DIR = OTP_DIR / "config"
CONFIG_FILES = ("otp-config.json", "build-config.json", "router-config.json")
CALENDAR_FIX = SCRIPT_DIR / "fix-gtfs-calendar-dates.py"
KEEP_VERSIONS = 2
GTFS_READ_ATTEMPTS = 5
# OTP определяет GTFS по имени файла (шаблон (?i)gtfs), а не по содержимому.
GTFS_FILENAME = "feed.gtfs.zip"
# Второй фид: оценочный GTFS метро из data.metro.refresh.
METRO_FILENAME = "metro.gtfs.zip"
# Меняется при изменении логики подготовки/сборки: старая версия графа
# перестаёт считаться эквивалентной и пересобирается.
PIPELINE_VERSION = 2


def log(message: str) -> None:
    print(f"[graph-store] {message}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"ERROR: ожидался JSON-объект: {path}")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as target:
            temporary = Path(target.name)
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        # NamedTemporaryFile создаёт 0600; метаданные должны читаться
        # backend и контейнером под другим пользователем.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def build_lock(data_dir: Path):
    """Неблокирующая блокировка: параллельная сборка не запускается."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("ERROR: другая сборка графа уже выполняется") from exc
        yield


def load_configs(config_dir: Path) -> dict[str, dict]:
    configs = {}
    for name in CONFIG_FILES:
        path = config_dir / name
        if not path.is_file():
            raise SystemExit(f"ERROR: нет конфигурации OTP: {path}")
        configs[name] = read_json(path)
    return configs


def normalized_config_sha(configs: dict[str, dict]) -> str:
    """Хэш конфигов без configVersion: версия ещё не известна на этом шаге."""
    normalized = {}
    for name, value in configs.items():
        normalized[name] = {**value, "configVersion": ""} if "configVersion" in value else value
    return sha256_json(normalized)


def graph_version(gtfs_fixed_sha: str, osm_sha: str, config_sha: str,
                  otp_version: str, otp_serialization_version_id: str,
                  metro_sha: str = "") -> str:
    return sha256_json({
        "pipeline_version": PIPELINE_VERSION,
        "gtfs_fixed_sha256": gtfs_fixed_sha,
        "metro_sha256": metro_sha,
        "osm_sha256": osm_sha,
        "config_sha256": config_sha,
        "otp_version": otp_version,
        "otp_serialization_version_id": otp_serialization_version_id,
    })[:16]


def resolve_gtfs(gtfs_zip: Path | None, gtfs_store: Path | None) -> tuple[Path, str | None]:
    """Найти ZIP: явный файл для локальной отладки или версия из data/gtfs.

    Хранилище удаляет старый снимок сразу после публикации нового. Если файл
    исчез между чтением указателя и открытием, указатель перечитывается.
    """
    if gtfs_zip is not None:
        if not gtfs_zip.is_file():
            raise SystemExit(f"ERROR: GTFS ZIP не найден: {gtfs_zip}")
        return gtfs_zip, None
    if gtfs_store is None:
        raise SystemExit("ERROR: укажите --gtfs-zip или --gtfs-store")
    for attempt in range(GTFS_READ_ATTEMPTS):
        pointer = read_json(gtfs_store / "current.json")
        digest = pointer.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise SystemExit(f"ERROR: некорректный указатель GTFS: {gtfs_store / 'current.json'}")
        feed = gtfs_store / "versions" / digest / "feed.zip"
        if feed.is_file():
            return feed, digest
        log(f"GTFS {digest[:12]} исчез между чтением указателя и копированием, повтор {attempt + 1}")
        time.sleep(1)
    raise SystemExit("ERROR: не удалось прочитать текущий GTFS из версионного хранилища")


def service_range(feed: Path) -> dict:
    """Диапазон дат, встречающихся в GTFS. Не равен serviceTimeRange OTP:
    OTP обрезает импорт по transitServiceStart/End, поэтому авторитетные
    значения service_start/service_end записывает команда verify.
    """
    dates: list[str] = []
    try:
        with zipfile.ZipFile(feed) as archive:
            if "calendar.txt" in archive.namelist():
                rows = archive.read("calendar.txt").decode("latin-1").split("\n")
                header = rows[0].split(",")
                try:
                    start_index = header.index("start_date")
                    end_index = header.index("end_date")
                except ValueError:
                    start_index = end_index = None
                if start_index is not None:
                    for row in rows[1:]:
                        fields = row.split(",")
                        if len(fields) > max(start_index, end_index):
                            dates.extend([fields[start_index], fields[end_index]])
            if "calendar_dates.txt" in archive.namelist():
                rows = archive.read("calendar_dates.txt").decode("latin-1").split("\n")
                header = rows[0].split(",")
                if "date" in header:
                    index = header.index("date")
                    for row in rows[1:]:
                        fields = row.split(",")
                        if len(fields) > index:
                            dates.append(fields[index])
    except (OSError, zipfile.BadZipFile):
        return {}
    valid = sorted({value for value in dates if len(value) == 8 and value.isdigit()})
    if not valid:
        return {}
    return {"gtfs_service_start": f"{valid[0][:4]}-{valid[0][4:6]}-{valid[0][6:]}",
            "gtfs_service_end": f"{valid[-1][:4]}-{valid[-1][4:6]}-{valid[-1][6:]}"}


def fix_calendar(feed: Path) -> bool:
    """Исправить инвертированные end_date в копии фида. Исходный ZIP не трогаем."""
    before = sha256_file(feed)
    result = subprocess.run([sys.executable, str(CALENDAR_FIX), str(feed)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("ERROR: не удалось исправить календарь GTFS:\n"
                         f"{result.stdout}{result.stderr}")
    changed = sha256_file(feed) != before
    if changed:
        log("календарь GTFS исправлен в копии staging")
    return changed


def write_configs(directory: Path, configs: dict[str, dict], version: str) -> None:
    for name, value in configs.items():
        if "configVersion" in value:
            value = {**value, "configVersion": version}
        atomic_json(directory / name, value)


def update_build_status(data_dir: Path, status: dict) -> None:
    previous = read_json(data_dir / "build-status.json")
    atomic_json(data_dir / "build-status.json", {**previous, **status})


def resolve_metro(metro_zip: Path | None, metro_store: Path | None) -> tuple[Path | None, str]:
    """Найти GTFS метро: явный файл или текущая версия из data/metro.

    Метро обязательно по умолчанию: без фида граф молча теряет подземку, а
    версия при этом совпадает с прежней наземной сборкой. Отключение — только
    явный ``--no-metro`` (в build-graph.sh это ``METRO_STORE=""``).
    """
    if metro_zip is not None:
        if not metro_zip.is_file():
            raise SystemExit(f"ERROR: GTFS метро не найден: {metro_zip}")
        return metro_zip, sha256_file(metro_zip)
    if metro_store is None:
        log("GTFS метро отключён (--no-metro): граф будет без метро")
        return None, ""
    if not (metro_store / "current.json").is_file():
        raise SystemExit(
            f"ERROR: нет указателя GTFS метро: {metro_store / 'current.json'}\n"
            f"Соберите фид: python -m data.metro.refresh --store-dir {metro_store}\n"
            "или соберите граф без метро: --no-metro (METRO_STORE='')")
    for attempt in range(GTFS_READ_ATTEMPTS):
        pointer = read_json(metro_store / "current.json")
        digest = pointer.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise SystemExit(f"ERROR: некорректный указатель GTFS метро: {metro_store / 'current.json'}")
        feed = metro_store / "versions" / digest / "feed.zip"
        if feed.is_file():
            return feed, digest
        log(f"GTFS метро {digest[:12]} исчез между чтением указателя и копированием, повтор {attempt + 1}")
        time.sleep(1)
    raise SystemExit("ERROR: не удалось прочитать текущий GTFS метро из версионного хранилища")


def prepare(args) -> int:
    data_dir = Path(args.data_dir).resolve()
    staging = data_dir / "staging"
    temporary = data_dir / "staging.tmp"
    configs = load_configs(Path(args.config_dir).resolve())
    with build_lock(data_dir):
        gtfs_path, store_sha = resolve_gtfs(
            Path(args.gtfs_zip).resolve() if args.gtfs_zip else None,
            Path(args.gtfs_store).resolve() if args.gtfs_store else None,
        )
        source_sha = sha256_file(gtfs_path)
        if store_sha is not None and store_sha != source_sha:
            raise SystemExit("ERROR: SHA256 GTFS не совпадает с указателем current.json")
        osm_path = Path(args.osm_pbf).resolve()
        if not osm_path.is_file():
            raise SystemExit(f"ERROR: OSM PBF не найден: {osm_path}")

        metro_path, metro_sha = resolve_metro(
            Path(args.metro_zip).resolve() if args.metro_zip else None,
            None if args.no_metro else Path(args.metro_store).resolve(),
        )

        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        feed = temporary / GTFS_FILENAME
        shutil.copyfile(gtfs_path, feed)
        calendar_fixed = fix_calendar(feed)
        fixed_sha = sha256_file(feed)
        if metro_path is not None:
            # OTP определяет тип файла по имени: шаблон (?i)gtfs.
            shutil.copyfile(metro_path, temporary / METRO_FILENAME)
        osm_sha = sha256_file(osm_path)
        config_sha = normalized_config_sha(configs)
        version = graph_version(fixed_sha, osm_sha, config_sha,
                                args.otp_version, args.otp_serialization_version_id,
                                metro_sha)

        published = data_dir / "versions" / version / "graph.obj"
        if published.is_file() and not args.force:
            shutil.rmtree(temporary)
            # Готовый граф мог остаться неактивным: publish прервался или
            # вернули предыдущую версию вручную. Иначе --restart молча
            # перезапускает контейнер на старом графе.
            activated = activate_published(data_dir, version)
            update_build_status(data_dir, {"status": "unchanged", "attempted_at": now_iso(),
                                           "version": version, "error": None})
            print(json.dumps({"status": "unchanged", "version": version,
                              "activated": activated, "graph": str(published)},
                             ensure_ascii=False))
            return 0

        write_configs(temporary, configs, version)
        manifest = {
            "version": version,
            "gtfs_source_sha256": source_sha,
            "gtfs_fixed_sha256": fixed_sha,
            "gtfs_calendar_fixed": calendar_fixed,
            "metro_sha256": metro_sha or None,
            "osm_sha256": osm_sha,
            "osm_file": osm_path.name,
            "config_sha256": config_sha,
            "otp_version": args.otp_version,
            "otp_serialization_version_id": args.otp_serialization_version_id,
            "prepared_at": now_iso(),
            **service_range(feed),
        }
        atomic_json(temporary / "manifest.prepared.json", manifest)

        if staging.exists():
            shutil.rmtree(staging)
        os.replace(temporary, staging)
        update_build_status(data_dir, {"status": "prepared", "attempted_at": now_iso(),
                                       "version": version, "error": None})
        print(json.dumps({"status": "prepared", **manifest,
                          "staging": str(staging)}, ensure_ascii=False))
        return 0


def switch_current(data_dir: Path, version: str) -> None:
    link = data_dir / "current"
    temporary = data_dir / "current.tmp"
    temporary.unlink(missing_ok=True)
    os.symlink(Path("versions") / version, temporary)
    os.replace(temporary, link)


def is_current(data_dir: Path, version: str) -> bool:
    link = data_dir / "current"
    return link.is_symlink() and Path(os.readlink(link)) == Path("versions") / version


def activate_published(data_dir: Path, version: str) -> bool:
    """Сделать уже собранную версию активной. Возвращает True, если переключили.

    В отличие от publish ничего не собирает и не переносит: каталог версии
    должен быть полным, иначе это ошибка, а не повод пересобирать.
    """
    if is_current(data_dir, version):
        return False
    manifest = read_json(data_dir / "versions" / version / "manifest.json")
    if not manifest.get("version"):
        raise SystemExit(f"ERROR: нет manifest.json опубликованной версии: {version}")
    switch_current(data_dir, version)
    atomic_json(data_dir / "current.json", {**manifest, "activated_at": None, "activated_by": None})
    prune(data_dir, version)
    log(f"версия {version} уже собрана — активирована без пересборки")
    return True


def prune(data_dir: Path, keep: str) -> list[str]:
    """Оставить активную и одну предыдущую версию: этого достаточно для отката."""
    versions_dir = data_dir / "versions"
    versions = sorted((path for path in versions_dir.iterdir() if path.is_dir()),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    protected = {keep}
    for path in versions:
        if len(protected) >= KEEP_VERSIONS:
            break
        protected.add(path.name)
    removed = []
    for path in versions:
        if path.name in protected:
            continue
        try:
            shutil.rmtree(path)
            removed.append(path.name)
        except OSError as exc:
            log(f"WARN: не удалось удалить старую версию {path.name}: {exc}")
    return removed


def publish(args) -> int:
    data_dir = Path(args.data_dir).resolve()
    staging = data_dir / "staging"
    with build_lock(data_dir):
        prepared = read_json(staging / "manifest.prepared.json")
        version = prepared.get("version")
        if not isinstance(version, str) or not version:
            raise SystemExit(f"ERROR: staging не подготовлен: {staging}")
        graph = staging / "graph.obj"
        if not graph.is_file() or graph.stat().st_size == 0:
            raise SystemExit("ERROR: сборка не создала graph.obj; версия не публикуется")

        manifest = {**prepared, "graph_sha256": sha256_file(graph),
                    "graph_bytes": graph.stat().st_size, "built_at": now_iso()}
        destination = data_dir / "versions" / version
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not (destination / "graph.obj").is_file():
                raise SystemExit(f"ERROR: каталог версии уже существует и неполон: {destination}")
            shutil.rmtree(staging)
            log(f"версия {version} уже опубликована")
        else:
            os.replace(staging, destination)
            atomic_json(destination / "manifest.json", manifest)
            (destination / "manifest.prepared.json").unlink(missing_ok=True)
            # Входные данные нужны только на время сборки. Нулевой .pbf —
            # плейсхолдер Docker, созданный под точку монтирования OSM.
            (destination / GTFS_FILENAME).unlink(missing_ok=True)
            (destination / METRO_FILENAME).unlink(missing_ok=True)
            for placeholder in destination.glob("*.pbf"):
                if placeholder.stat().st_size == 0:
                    placeholder.unlink(missing_ok=True)

        switch_current(data_dir, version)
        current = {**manifest, "activated_at": None, "activated_by": None}
        atomic_json(data_dir / "current.json", current)
        removed = prune(data_dir, version)
        update_build_status(data_dir, {"status": "updated", "attempted_at": manifest["built_at"],
                                       "last_success_at": manifest["built_at"],
                                       "version": version, "error": None})
        print(json.dumps({"status": "updated", "version": version,
                          "graph": str(destination / "graph.obj"),
                          "pruned": removed,
                          "next": "docker compose up -d --force-recreate otp"},
                         ensure_ascii=False))
        return 0


def graphql(url: str, query: str, variables: dict | None = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(f"{url.rstrip('/')}/otp/gtfs/v1", data=payload,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def verify(args) -> int:
    """Проверить, что OTP загрузил именно опубликованную версию графа."""
    data_dir = Path(args.data_dir).resolve()
    current = read_json(data_dir / "current.json")
    version = current.get("version")
    if not version:
        raise SystemExit("ERROR: current.json отсутствует или не содержит version")
    url = args.url
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/otp/", timeout=15) as response:
            info = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: OTP недоступен по {url}: {exc}") from exc
    config = info.get("config") or {}
    loaded = {config.get("buildConfigVersion"), config.get("routerConfigVersion")}
    if version not in loaded:
        raise SystemExit(f"ERROR: OTP загрузил другую версию конфигов: {sorted(v for v in loaded if v)}; "
                         f"ожидалась {version}. Пересоздайте контейнер OTP.")

    try:
        data = graphql(url, "{ serviceTimeRange { start end } }")["data"]["serviceTimeRange"]
        service = {}
        for key in ("start", "end"):
            value = data.get(key)
            if isinstance(value, (int, float)):
                service[f"service_{key}"] = datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        service = {}

    updated = {**current, "activated_at": now_iso(), "activated_by": "graph_store.verify", **service}
    atomic_json(data_dir / "current.json", updated)
    print(json.dumps({"status": "activated", **updated}, ensure_ascii=False))
    return 0


def status(args) -> int:
    data_dir = Path(args.data_dir).resolve()
    print(json.dumps({
        "current": read_json(data_dir / "current.json"),
        "build_status": read_json(data_dir / "build-status.json"),
        "versions": sorted(path.name for path in (data_dir / "versions").iterdir()
                           if path.is_dir()) if (data_dir / "versions").is_dir() else [],
        "current_link": os.readlink(data_dir / "current") if (data_dir / "current").is_symlink() else None,
    }, ensure_ascii=False, indent=2))
    return 0


def mark(args) -> int:
    """Зафиксировать неудачную попытку сборки, не трогая активную версию."""
    data_dir = Path(args.data_dir).resolve()
    update_build_status(data_dir, {"status": args.status, "attempted_at": now_iso(),
                                   "error": args.error})
    print(json.dumps(read_json(data_dir / "build-status.json"), ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Версионное хранилище графа OTP")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare", help="подготовить staging и вычислить версию")
    prepare_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    prepare_parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    prepare_parser.add_argument("--osm-pbf", default=str(DEFAULT_DATA_DIR / "osm" / "spb-lo.osm.pbf"))
    prepare_parser.add_argument("--gtfs-zip", help="явный GTFS ZIP для локальной отладки")
    prepare_parser.add_argument("--gtfs-store", help="каталог версионного хранилища GTFS")
    metro_source = prepare_parser.add_mutually_exclusive_group()
    metro_source.add_argument("--metro-zip", help="явный GTFS ZIP метро")
    metro_source.add_argument("--no-metro", action="store_true",
                              help="собрать граф без метро (явное отключение)")
    prepare_parser.add_argument("--metro-store", default=str(ROOT_DIR / "data" / "metro"),
                                help="каталог версионного хранилища GTFS метро")
    prepare_parser.add_argument("--otp-version", default="unknown")
    prepare_parser.add_argument("--otp-serialization-version-id", default="unknown")
    prepare_parser.add_argument("--force", action="store_true", help="пересобрать существующую версию")
    prepare_parser.set_defaults(func=prepare)

    publish_parser = sub.add_parser("publish", help="опубликовать собранный граф")
    publish_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    publish_parser.set_defaults(func=publish)

    verify_parser = sub.add_parser("verify", help="подтвердить загрузку версии в OTP")
    verify_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    verify_parser.add_argument("--url", default="http://localhost:8080")
    verify_parser.set_defaults(func=verify)

    status_parser = sub.add_parser("status", help="показать активную версию и статус сборки")
    status_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    status_parser.set_defaults(func=status)

    mark_parser = sub.add_parser("mark", help="записать статус неудачной сборки")
    mark_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    mark_parser.add_argument("--status", default="error", choices=("error", "prepared"))
    mark_parser.add_argument("--error", default=None)
    mark_parser.set_defaults(func=mark)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
