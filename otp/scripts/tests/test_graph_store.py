"""Тесты версионного хранилища графа OTP; сеть и Docker не используются."""
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import graph_store as store  # noqa: E402


def make_feed(path: Path, *, start: str = "20260801", end: str = "20291109") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("calendar.txt",
                         "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\n"
                         f"1,1,1,1,1,1,0,0,{start},{end}\n")
        archive.writestr("calendar_dates.txt", "service_id,date,exception_type\n"
                                              "1,20261104,1\n")
        archive.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n")
    return path


def test_version_is_deterministic_and_covers_all_inputs():
    base = store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.11", "289")
    assert base == store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.11", "289")
    assert len(base) == 16
    assert base != store.graph_version("d" * 64, "b" * 64, "c" * 64, "2.11", "289")
    assert base != store.graph_version("a" * 64, "d" * 64, "c" * 64, "2.11", "289")
    assert base != store.graph_version("a" * 64, "b" * 64, "d" * 64, "2.11", "289")
    assert base != store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.12", "289")
    assert base != store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.11", "300")


def test_metro_feed_changes_graph_version():
    """Обновление только метро обязано пересобрать граф."""
    surface = store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.11", "289")
    with_metro = store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.11", "289", "m" * 64)
    assert with_metro != surface
    assert with_metro != store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.11", "289", "n" * 64)
    assert store.graph_version("a" * 64, "b" * 64, "c" * 64, "2.11", "289", "") == surface


def test_config_hash_ignores_config_version():
    configs = {"router-config.json": {"configVersion": "local", "updaters": []}}
    assert store.normalized_config_sha(configs) == store.normalized_config_sha(
        {"router-config.json": {"configVersion": "other", "updaters": []}})
    assert store.normalized_config_sha(configs) != store.normalized_config_sha(
        {"router-config.json": {"configVersion": "local", "updaters": [{"type": "x"}]}})


def test_publish_is_atomic_and_requires_graph(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "manifest.prepared.json").write_text(json.dumps({"version": "v1"}))
    with pytest.raises(SystemExit):
        store.publish(_args(tmp_path))
    assert not (tmp_path / "current.json").exists()
    assert not (tmp_path / "current").exists()

    (staging / "graph.obj").write_bytes(b"graph")
    store.publish(_args(tmp_path))
    assert (tmp_path / "current").is_symlink()
    assert (tmp_path / "current").resolve() == (tmp_path / "versions" / "v1").resolve()
    current = store.read_json(tmp_path / "current.json")
    assert current["version"] == "v1"
    assert current["activated_at"] is None
    assert current["graph_sha256"] == store.sha256_file(tmp_path / "versions" / "v1" / "graph.obj")


def test_publish_keeps_previous_version_for_rollback(tmp_path):
    for version in ("v1", "v2", "v3"):
        staging = tmp_path / "staging"
        staging.mkdir(exist_ok=True)
        (staging / "graph.obj").write_bytes(version.encode())
        (staging / "manifest.prepared.json").write_text(json.dumps({"version": version}))
        store.publish(_args(tmp_path))
    assert sorted(p.name for p in (tmp_path / "versions").iterdir()) == ["v2", "v3"]
    assert store.read_json(tmp_path / "current.json")["version"] == "v3"


def test_service_range_reads_calendar_and_exceptions(tmp_path):
    feed = make_feed(tmp_path / "feed.zip")
    assert store.service_range(feed) == {"gtfs_service_start": "2026-08-01",
                                         "gtfs_service_end": "2029-11-09"}
    assert store.service_range(make_feed(tmp_path / "bad.zip", start="20291231",
                                         end="20260101"))["gtfs_service_end"] == "2029-12-31"


def test_gtfs_filename_matches_otp_pattern():
    # OTP ищет GTFS по шаблону (?i)gtfs в имени файла, а не по содержимому.
    assert "gtfs" in store.GTFS_FILENAME.lower()
    assert store.GTFS_FILENAME.endswith(".zip")
    assert "gtfs" in store.METRO_FILENAME.lower()


def make_metro_store(root: Path, digest: str = "a" * 64) -> Path:
    """Версионное хранилище метро в том же формате, что пишет data.metro.refresh."""
    version = root / "versions" / digest
    version.mkdir(parents=True)
    make_feed(version / "feed.zip")
    (root / "current.json").write_text(json.dumps({"sha256": digest}))
    return root


def test_resolve_metro_prefers_explicit_zip(tmp_path):
    metro = make_feed(tmp_path / "metro.zip")
    path, digest = store.resolve_metro(metro, tmp_path / "ignored-store")
    assert path == metro
    assert digest == store.sha256_file(metro)


def test_resolve_metro_reads_versioned_store(tmp_path):
    feed_store = make_metro_store(tmp_path / "metro")
    path, digest = store.resolve_metro(None, feed_store)
    assert path == feed_store / "versions" / ("a" * 64) / "feed.zip"
    assert digest == "a" * 64


def test_resolve_metro_fails_when_feed_is_expected_but_missing(tmp_path):
    """Пропавший фид — ошибка: иначе граф молча теряет метро."""
    with pytest.raises(SystemExit) as error:
        store.resolve_metro(None, tmp_path / "empty-store")
    assert "data.metro.refresh" in str(error.value)
    with pytest.raises(SystemExit):
        store.resolve_metro(tmp_path / "absent.zip", None)


def test_resolve_metro_can_be_disabled_explicitly():
    assert store.resolve_metro(None, None) == (None, "")


def test_publish_removes_build_only_feeds(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "graph.obj").write_bytes(b"graph")
    (staging / "manifest.prepared.json").write_text(json.dumps({"version": "v1"}))
    (staging / store.GTFS_FILENAME).write_bytes(b"surface")
    (staging / store.METRO_FILENAME).write_bytes(b"metro")
    store.publish(_args(tmp_path))
    version = tmp_path / "versions" / "v1"
    assert (version / "graph.obj").is_file()
    assert not (version / store.GTFS_FILENAME).exists()
    assert not (version / store.METRO_FILENAME).exists()


def test_build_status_is_merged_not_replaced(tmp_path):
    store.update_build_status(tmp_path, {"status": "prepared", "version": "v1"})
    store.update_build_status(tmp_path, {"status": "error", "error": "boom"})
    status = store.read_json(tmp_path / "build-status.json")
    assert status == {"status": "error", "version": "v1", "error": "boom"}


def test_build_lock_is_non_blocking(tmp_path):
    with store.build_lock(tmp_path):
        with pytest.raises(SystemExit):
            with store.build_lock(tmp_path):
                pass


def test_activate_published_switches_pointer_back(tmp_path):
    """Откат на предыдущую версию не требует пересборки."""
    for version in ("v1", "v2"):
        staging = tmp_path / "staging"
        staging.mkdir(exist_ok=True)
        (staging / "graph.obj").write_bytes(version.encode())
        (staging / "manifest.prepared.json").write_text(json.dumps({"version": version}))
        store.publish(_args(tmp_path))

    assert store.activate_published(tmp_path, "v1") is True
    assert store.is_current(tmp_path, "v1")
    assert store.read_json(tmp_path / "current.json")["version"] == "v1"
    assert store.read_json(tmp_path / "current.json")["activated_at"] is None
    assert store.activate_published(tmp_path, "v1") is False


def test_activate_published_rejects_incomplete_version(tmp_path):
    (tmp_path / "versions" / "v1").mkdir(parents=True)
    with pytest.raises(SystemExit):
        store.activate_published(tmp_path, "v1")
    assert not (tmp_path / "current").exists()


def _prepare_args(root: Path, configs: Path, feed: Path, osm: Path, metro: Path):
    class Args:
        data_dir = str(root)
        config_dir = str(configs)
        osm_pbf = str(osm)
        gtfs_zip = str(feed)
        gtfs_store = None
        metro_zip = str(metro)
        metro_store = str(root / "unused-metro-store")
        no_metro = False
        otp_version = "2.11.0-SNAPSHOT"
        otp_serialization_version_id = "289"
        force = False
    return Args()


def test_prepare_copies_metro_feed_and_activates_published_version(tmp_path, capsys):
    configs = tmp_path / "config"
    configs.mkdir()
    for name in store.CONFIG_FILES:
        (configs / name).write_text("{}")
    feed = make_feed(tmp_path / "feed.zip")
    metro = make_feed(tmp_path / "metro.zip")
    osm = tmp_path / "region.osm.pbf"
    osm.write_bytes(b"osm")
    args = _prepare_args(tmp_path / "data", configs, feed, osm, metro)

    store.prepare(args)
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "prepared"
    staging = Path(args.data_dir) / "staging"
    # OTP находит фид по имени файла: метро обязано попасть в staging.
    assert (staging / store.METRO_FILENAME).is_file()
    assert first["metro_sha256"] == store.sha256_file(metro)

    # Имитируем успешную сборку: publish переносит staging в versions/<version>.
    version = first["version"]
    published = Path(args.data_dir) / "versions" / version
    published.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(published)
    (published / "graph.obj").write_bytes(b"graph")
    (published / "manifest.prepared.json").rename(published / "manifest.json")

    store.prepare(args)
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "unchanged"
    assert second["activated"] is True
    assert store.is_current(Path(args.data_dir), version)
    assert store.read_json(Path(args.data_dir) / "current.json")["version"] == version

    store.prepare(args)
    third = json.loads(capsys.readouterr().out)
    assert third["status"] == "unchanged"
    assert third["activated"] is False


def test_prepare_without_metro_requires_explicit_flag(tmp_path):
    configs = tmp_path / "config"
    configs.mkdir()
    for name in store.CONFIG_FILES:
        (configs / name).write_text("{}")
    osm = tmp_path / "region.osm.pbf"
    osm.write_bytes(b"osm")
    args = _prepare_args(tmp_path / "data", configs, make_feed(tmp_path / "feed.zip"), osm,
                         make_feed(tmp_path / "metro.zip"))
    args.metro_zip = None
    args.metro_store = str(tmp_path / "missing-store")
    with pytest.raises(SystemExit) as error:
        store.prepare(args)
    assert "нет указателя GTFS метро" in str(error.value)

    args.no_metro = True
    store.prepare(args)
    assert not (Path(args.data_dir) / "staging" / store.METRO_FILENAME).exists()


def _args(root: Path):
    class Args:
        data_dir = str(root)
    return Args()
