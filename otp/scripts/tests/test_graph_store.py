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


def _args(root: Path):
    class Args:
        data_dir = str(root)
    return Args()
