import json
import shutil
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import data_status


REQUEST = {"points": [{"lat": 60, "lon": 30}, {"lat": 61, "lon": 31}],
           "departure_at": "2026-11-10T15:00:00+03:00"}


@pytest.mark.parametrize("patch", [
    {"points": None}, {"points": [{"lat": None, "lon": 30}, {"lat": 61, "lon": 31}]},
    {"points": [{"lat": -90.01, "lon": 30}, {"lat": 61, "lon": 31}]},
    {"points": [{"lat": 60, "lon": 180.01}, {"lat": 61, "lon": 31}]},
    {"departure_at": "not-a-date"}, {"departure_at": "2026-02-30T10:00:00Z"},
    {"passenger": None}, {"passenger": {"ticket_profile": "student"}},
    {"passenger": {"ticket_profile": "full_fare", "price": 0}},
    {"max_results": 0}, {"max_results": 11}, {"traffic_mode": "unknown"},
])
def test_invalid_request_edges(tmp_path, patch):
    client = TestClient(create_app(tmp_path, "dummy"))
    assert client.post("/api/v1/routes", json={**REQUEST, **patch}).status_code == 422


@pytest.mark.parametrize("points", [
    [{"lat": -90, "lon": -180}, {"lat": 90, "lon": 180}],
    [{"lat": 90, "lon": 180}, {"lat": -90, "lon": -180}],
    [{"lat": 60, "lon": 30}, {"lat": 60, "lon": 30}],
])
def test_dummy_geometry_and_leg_times_at_coordinate_bounds(tmp_path, points):
    client = TestClient(create_app(tmp_path, "dummy"))
    response = client.post("/api/v1/routes", json={**REQUEST, "points": points, "traffic_mode": "on"})
    assert response.status_code == 200
    data = response.json()
    assert data["passenger"] == {"ticket_profile": "bank_card", "uncovered_payment": "bank_card"}
    for leg in data["routes"][0]["legs"]:
        assert leg["geometry"]["type"] == "LineString"
        assert leg["geometry"]["coordinates"][0] == [leg["from"]["lon"], leg["from"]["lat"]]
        assert leg["geometry"]["coordinates"][-1] == [leg["to"]["lon"], leg["to"]["lat"]]
        assert leg["duration_seconds"] == leg["original_duration_seconds"]
        assert (datetime.fromisoformat(leg["end_at"]) - datetime.fromisoformat(leg["start_at"])).total_seconds() == leg["duration_seconds"]
        assert leg["duration_source"] == "dummy"


def test_unrepresentable_dummy_arrival_is_validation_error(tmp_path):
    client = TestClient(create_app(tmp_path, "dummy"))
    response = client.post("/api/v1/routes", json={**REQUEST, "departure_at": "9999-12-31T23:59:59Z"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_departure_time"


@pytest.mark.parametrize("file, contents", [
    ("gtfs/current.json", "{"),
    ("gtfs/current.json", "[]"),
    ("gtfs/current.json", '{"sha256":123}'),
    ("gtfs/refresh-status.json", "null"),
    ("otp/current.json", '{"version":123}'),
    ("gtfs/current.json", '{"sha256":"' + "a" * 64 + '","active_service_ids":"0"}'),
])
def test_corrupt_status_never_crashes_api(tmp_path, file, contents):
    gtfs = tmp_path / "gtfs"
    version = gtfs / "versions" / ("a" * 64)
    version.mkdir(parents=True)
    (gtfs / "current.json").write_text(json.dumps({"sha256": "a" * 64}))
    (version / "manifest.json").write_text(json.dumps({"active_service_ids": 2, "service_date": "20261110"}))
    target = tmp_path / file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    client = TestClient(create_app(tmp_path, "dummy"))
    response = client.get("/api/v1/data/status")
    assert response.status_code == 200 and response.json()["state"] == "degraded"
    assert client.get("/health/live").status_code == 200
    assert client.post("/api/v1/routes", json=REQUEST).status_code == 200


def test_current_calendar_overrides_ingestion_manifest_and_moscow_date(tmp_path):
    gtfs = tmp_path / "gtfs"
    version = gtfs / "versions" / ("a" * 64)
    version.mkdir(parents=True)
    (version / "manifest.json").write_text(json.dumps({"active_service_ids": 10, "service_date": "20261109"}))
    (gtfs / "current.json").write_text(json.dumps({"sha256": "a" * 64, "active_service_ids": 0, "service_date": "20261110"}))
    (gtfs / "refresh-status.json").write_text(json.dumps({"status": "error"}))
    client = TestClient(create_app(tmp_path, "unavailable"))
    status = client.get("/api/v1/data/status").json()
    assert status["manifest"]["service_date"] == "20261110"
    assert status["manifest"]["active_service_ids"] == 0
    assert any("refresh failed" in warning for warning in status["warnings"])
    # UTC evening is already the next service day in Moscow.
    response = client.post("/api/v1/routes?dummy=false", json={**REQUEST, "departure_at": "2026-11-09T22:00:00Z"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_service_date"
    response = client.post("/api/v1/routes?dummy=false", json={**REQUEST, "departure_at": "2026-11-08T22:00:00Z"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "planner_unavailable"


def test_status_remains_consistent_when_updater_removes_previous_payload(tmp_path, monkeypatch):
    """A publish between pointer and manifest reads must not degrade status."""
    gtfs = tmp_path / "gtfs"
    old_version = gtfs / "versions" / ("a" * 64)
    old_version.mkdir(parents=True)
    (old_version / "manifest.json").write_text('{"active_service_ids":4}')
    old = {"sha256": "a" * 64, "active_service_ids": 4, "service_date": "20261110"}
    new = {"sha256": "b" * 64, "active_service_ids": 8, "service_date": "20261110"}
    pointer = gtfs / "current.json"
    pointer.write_text(json.dumps(old))
    original_read = data_status.read_object
    reads = []

    def publish_after_read(path):
        reads.append(path)
        value = original_read(path)
        if path == pointer:
            # Emulate atomic publish followed by deletion, after this request
            # captured the old metadata but before it can read another file.
            incoming = gtfs / "current.next.json"
            incoming.write_text(json.dumps(new))
            incoming.replace(pointer)
            if old_version.exists():
                shutil.rmtree(old_version)
        return value

    monkeypatch.setattr(data_status, "read_object", publish_after_read)
    client = TestClient(create_app(tmp_path, "unavailable"))
    first = client.get("/api/v1/data/status").json()
    assert first["state"] == "ready"
    assert first["gtfs_version"] == first["manifest"]["sha256"] == old["sha256"]
    assert first["manifest"]["active_service_ids"] == 4
    second = client.get("/api/v1/data/status").json()
    assert second["state"] == "ready"
    assert second["gtfs_version"] == second["manifest"]["sha256"] == new["sha256"]
    assert second["manifest"]["active_service_ids"] == 8
    assert not any("versions" in path.parts for path in reads)
