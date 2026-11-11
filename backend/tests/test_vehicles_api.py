from datetime import timedelta
import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import vehicles_api
from app.vehicles_api import DEMO_TIMESTAMP, create_vehicles_router
from app.vehicles_schemas import VehiclePosition


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("Vehicles must not access external network"))


def client(root, *, clock=None):
    app = FastAPI()
    app.include_router(create_vehicles_router(root, clock=clock))
    return TestClient(app)


def positions():
    return [VehiclePosition(vehicle_id=f"dummy:test:{i}", route_id="test:line", trip_id=f"test:trip:{i}",
                            lat=lat, lon=lon, mode=mode, timestamp=DEMO_TIMESTAMP)
            for i, (lat, lon, mode) in enumerate([
                (60, 30, "bus"), (60.1, 30.1, "tram"), (60.2, 30.2, "bus"), (61, 31, "bus"),
            ])]


def test_dummy_default_filtering_precedes_truncation_and_bbox_is_inclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(vehicles_api, "demo_positions", lambda _: positions())
    api = client(tmp_path, clock=lambda: DEMO_TIMESTAMP + timedelta(seconds=61))
    response = api.get("/api/v1/vehicles", params={"bbox": "30,60,30.2,60.2", "modes": "bus", "limit": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "dummy" and data["stale"] is True
    assert data["updated_at"] == "2026-11-11T09:00:00Z"
    assert [p["vehicle_id"] for p in data["items"]] == ["dummy:test:0"]
    assert "vehicles_truncated" in data["warnings"]
    # The tram is the second original item: modes must filter before applying limit.
    data = api.get("/api/v1/vehicles", params={"bbox": "30,60,30.2,60.2", "modes": "tram", "limit": 1}).json()
    assert [p["vehicle_id"] for p in data["items"]] == ["dummy:test:1"]
    assert "vehicles_truncated" not in data["warnings"]
    assert data["items"][0]["heading"] is None and data["items"][0]["speed_mps"] is None


def test_no_matches_is_empty_success_and_multiple_modes_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(vehicles_api, "demo_positions", lambda _: positions())
    api = client(tmp_path)
    data = api.get("/api/v1/vehicles", params={"bbox": "30,60,30.2,60.2", "modes": "metro,rail"}).json()
    assert data["mode"] == "dummy" and data["items"] == []
    assert "vehicles_truncated" not in data["warnings"]
    data = api.get("/api/v1/vehicles", params={"bbox": "30,60,30.2,60.2", "modes": "bus, tram"}).json()
    assert len(data["items"]) == 3


@pytest.mark.parametrize("seconds, stale", [(30, False), (60, False), (61, True)])
def test_freshness_uses_fixed_demo_timestamp(tmp_path, monkeypatch, seconds, stale):
    monkeypatch.setattr(vehicles_api, "demo_positions", lambda _: [])
    api = client(tmp_path, clock=lambda: DEMO_TIMESTAMP + timedelta(seconds=seconds))
    data = api.get("/api/v1/vehicles", params={"bbox": "29,59,31,61"}).json()
    assert data["stale"] is stale
    assert data["updated_at"] == "2026-11-11T09:00:00Z"


@pytest.mark.parametrize("params", [
    {}, {"bbox": "30,60,30,61"}, {"bbox": "31,60,30,61"}, {"bbox": "30,61,31,60"},
    {"bbox": "-181,59,30,60"}, {"bbox": "30,59,31,91"}, {"bbox": "30,NaN,31,61"},
    {"bbox": "30,60,31"}, {"bbox": "30,60,31,61,62"},
    {"bbox": "30,60,31,61", "modes": "car"}, {"bbox": "30,60,31,61", "modes": ""},
    {"bbox": "30,60,31,61", "modes": "bus,,tram"},
    {"bbox": "30,60,31,61", "limit": 0}, {"bbox": "30,60,31,61", "limit": 2001},
    {"bbox": "30,60,31,61", "dummy": "maybe"},
])
def test_invalid_query_is_validation_error(tmp_path, monkeypatch, params):
    monkeypatch.setattr(vehicles_api, "demo_positions", lambda _: pytest.fail("Invalid query reached data"))
    assert client(tmp_path).get("/api/v1/vehicles", params=params).status_code == 422


def test_real_mode_is_explicitly_unavailable_and_does_not_read_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(vehicles_api, "demo_positions", lambda _: pytest.fail("Real mode must not fabricate markers"))
    response = client(tmp_path).get("/api/v1/vehicles", params={"bbox": "29,59,31,61", "dummy": False})
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "realtime_unavailable", "message": "GTFS-RT vehicle positions are not configured"}}


def test_shared_demo_positions_reference_real_demo_trips_and_stops(tmp_path):
    from app.gtfs_store import GtfsStore

    items = vehicles_api.demo_positions(tmp_path)
    assert items
    with GtfsStore(tmp_path).open(dummy=True) as snapshot:
        for item in items:
            assert item.vehicle_id.startswith("dummy:")
            assert item.route_id in snapshot.routes and item.trip_id in snapshot.trips
            trip, _ = snapshot.trip(item.trip_id)
            assert item.route_id == trip.route_id
            assert any((item.lat, item.lon) == (st.stop.lat, st.stop.lon) for st in trip.stop_times)
