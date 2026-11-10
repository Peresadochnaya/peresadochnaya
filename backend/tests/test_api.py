import json
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app


REQUEST = {"points": [{"lat": 60.002, "lon": 30.296}, {"lat": 60.036, "lon": 30.445}],
           "departure_at": "2026-11-10T15:00:00+03:00"}


def test_dummy_is_explicit_and_timeline_consistent(tmp_path):
    client = TestClient(create_app(tmp_path, "dummy"))
    response = client.post("/api/v1/routes", json=REQUEST)
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "dummy" and data["gtfs_version"] is None and data["warnings"]
    route = data["routes"][0]
    assert route["transfers"] == 1
    assert sum(leg["duration_seconds"] for leg in route["legs"]) == route["duration_seconds"]
    for left, right in zip(route["legs"], route["legs"][1:]):
        assert left["end_at"] == right["start_at"]
    assert (datetime.fromisoformat(route["end_at"]) - datetime.fromisoformat(route["start_at"])).total_seconds() == 1500


def test_request_rejects_naive_time_invalid_coords_and_unknown_fields(tmp_path):
    client = TestClient(create_app(tmp_path, "dummy"))
    assert client.post("/api/v1/routes", json={**REQUEST, "departure_at": "2026-11-10T15:00:00"}).status_code == 422
    assert client.post("/api/v1/routes", json={**REQUEST, "points": [{"lat": 91, "lon": 30}, REQUEST["points"][1]]}).status_code == 422
    assert client.post("/api/v1/routes", json={**REQUEST, "extra": True}).status_code == 422


def test_exactly_two_ordered_points_required(tmp_path):
    client = TestClient(create_app(tmp_path, "dummy"))
    for points in ([], REQUEST["points"][:1], REQUEST["points"] + REQUEST["points"][:1]):
        assert client.post("/api/v1/routes", json={**REQUEST, "points": points}).status_code == 422
    old_request = {"from": REQUEST["points"][0], "to": REQUEST["points"][1], "departure_at": REQUEST["departure_at"]}
    assert client.post("/api/v1/routes", json=old_request).status_code == 422
    route = client.post("/api/v1/routes", json=REQUEST).json()["routes"][0]
    assert {key: route["legs"][0]["from"][key] for key in ("lat", "lon")} == REQUEST["points"][0]
    assert {key: route["legs"][-1]["to"][key] for key in ("lat", "lon")} == REQUEST["points"][1]


def test_ticket_profiles_are_declared_without_fare_claims(tmp_path):
    client = TestClient(create_app(tmp_path, "dummy"))
    profiles = client.get("/api/v1/ticket-profiles").json()
    assert len(profiles) == 12
    for profile in profiles:
        assert profile["coverage_status"] == "not_implemented"
        passenger = {"ticket_profile": profile["id"]}
        data = client.post("/api/v1/routes", json={**REQUEST, "passenger": passenger}).json()
        assert data["passenger"] == passenger
        assert any("fare" in warning and "not calculated" in warning for warning in data["warnings"])
        assert "price" not in data["routes"][0]
    assert client.post("/api/v1/routes", json={**REQUEST, "passenger": {"ticket_profile": "unknown"}}).status_code == 422


def test_missing_data_and_unavailable_planner(tmp_path):
    client = TestClient(create_app(tmp_path, "unavailable"))
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/v1/data/status").json()["state"] == "missing"
    response = client.post("/api/v1/routes", json=REQUEST)
    assert response.status_code == 503 and response.json()["error"]["code"] == "planner_unavailable"


def test_versioned_status_invalid_calendar_and_corrupt_pointer(tmp_path):
    gtfs = tmp_path / "gtfs"
    version = gtfs / "versions" / ("a" * 64)
    version.mkdir(parents=True)
    (gtfs / "current.json").write_text(json.dumps({"sha256": "a" * 64, "active_service_ids": 0, "service_date": "20261110"}))
    (version / "manifest.json").write_text(json.dumps({"active_service_ids": 0, "service_date": "20261110"}))
    client = TestClient(create_app(tmp_path, "unavailable"))
    assert client.get("/api/v1/data/status").json()["state"] == "degraded"
    response = client.post("/api/v1/routes", json=REQUEST)
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_service_date"
    (gtfs / "current.json").write_text('{"sha256":"../../outside"}')
    assert client.get("/api/v1/data/status").json()["state"] == "degraded"
