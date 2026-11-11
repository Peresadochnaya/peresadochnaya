"""OTP adapter regressions: recorded itinerary, errors and status metadata."""
import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.otp import OtpPlanner, decode_polyline

FIXTURE = Path(__file__).resolve().parent.parent / "app" / "fixtures" / "otp" / "plan-response.json"
REQUEST = {"points": [{"lat": 59.9343, "lon": 30.3351}, {"lat": 59.9447, "lon": 30.2583}],
           "departure_at": "2026-11-11T12:00:00+03:00"}
GTFS_SHA = "a" * 64


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("OTP tests must not access network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def data_root(tmp_path: Path, *, graph: dict | None = None) -> Path:
    gtfs = tmp_path / "gtfs"
    gtfs.mkdir(parents=True, exist_ok=True)
    (gtfs / "current.json").write_text(json.dumps({
        "sha256": GTFS_SHA, "active_service_ids": 5, "service_date": "2026-11-11"}))
    otp = tmp_path / "otp"
    otp.mkdir(parents=True, exist_ok=True)
    (otp / "current.json").write_text(json.dumps(graph or {
        "version": "d58c053a487d1c7e", "gtfs_source_sha256": GTFS_SHA,
        "activated_at": "2026-11-11T11:00:38+03:00",
        "service_start": "2025-12-10T21:00:00+00:00", "service_end": "2029-11-09T21:00:00+00:00"}))
    return tmp_path


def client(root: Path, handler) -> TestClient:
    planner = OtpPlanner("http://otp:8080", client=httpx.Client(transport=httpx.MockTransport(handler)))
    return TestClient(create_app(root, "otp", transit_planner=planner))


def test_recorded_itinerary_maps_to_public_contract(tmp_path):
    body = payload()
    node = body["data"]["planConnection"]["edges"][0]["node"]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    response = client(data_root(tmp_path), handler).post("/api/v1/routes?dummy=false", json=REQUEST)
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "planner"
    assert data["gtfs_version"] == GTFS_SHA and data["graph_version"] == "d58c053a487d1c7e"
    assert any("Ticket profile is recorded only" in warning for warning in data["warnings"])
    route = data["routes"][0]
    assert route["transfers"] == node["numberOfTransfers"]
    assert route["start_at"] == node["start"] and route["end_at"] == node["end"]
    assert route["duration_seconds"] == node["duration"]
    assert route["distance_meters"] == pytest.approx(sum(leg["distance"] for leg in node["legs"]), abs=0.01)
    assert route["name"].startswith("Автобус")

    modes = [leg["mode"] for leg in route["legs"]]
    assert "wait" in modes and "bus" in modes and "walk" in modes
    assert modes[0] == "walk" and modes[-1] == "walk"

    # The recorded itinerary has a transfer gap; a wait leg must cover it and
    # keep the timeline continuous.
    for left, right in zip(route["legs"], route["legs"][1:]):
        assert left["end_at"] == right["start_at"]
    timeline = sum(leg["duration_seconds"] for leg in route["legs"])
    assert timeline == pytest.approx(route["duration_seconds"], abs=2)

    for leg in route["legs"]:
        assert leg["duration_source"] == "schedule"
        assert leg["duration_seconds"] == leg["original_duration_seconds"]
        assert leg["geometry"]["type"] == "LineString"
        assert len(leg["geometry"]["coordinates"]) >= 2
    wait = next(leg for leg in route["legs"] if leg["mode"] == "wait")
    assert wait["line"] is None
    assert wait["geometry"]["coordinates"][0] == wait["geometry"]["coordinates"][1]

    # Full route geometry follows legs in order without duplicating joints.
    expected = []
    for leg in route["legs"]:
        for position in leg["geometry"]["coordinates"]:
            if not expected or expected[-1] != position:
                expected.append(position)
    assert route["geometry"]["coordinates"] == expected

    variables = requests[0]["variables"]
    assert variables["first"] == 5
    assert variables["dateTime"]["earliestDeparture"] == "2026-11-11T12:00:00+03:00"
    assert [item["mode"] for item in variables["modes"]["transit"]["transit"]] == [
        "BUS", "TRAM", "TROLLEYBUS", "SUBWAY", "RAIL"]


def test_outside_service_period_is_a_warning_not_a_crash(tmp_path):
    body = payload()
    connection = body["data"]["planConnection"]
    connection["routingErrors"] = [{"code": "OUTSIDE_SERVICE_PERIOD",
                                    "description": "No transit times available",
                                    "inputField": "DATE_TIME"}]
    connection["edges"] = [{"node": {
        "start": "2030-01-01T12:00:00+03:00", "end": "2030-01-01T13:00:00+03:00",
        "duration": 3600, "numberOfTransfers": 0, "legs": [{
            "mode": "WALK", "transitLeg": False, "distance": 100.0,
            "start": {"scheduledTime": "2030-01-01T12:00:00+03:00", "estimated": None},
            "end": {"scheduledTime": "2030-01-01T13:00:00+03:00", "estimated": None},
            "from": {"name": "A", "lat": 59.93, "lon": 30.33, "stop": None},
            "to": {"name": "B", "lat": 59.94, "lon": 30.26, "stop": None},
            "route": None, "trip": None,
            "legGeometry": None}]}}]

    response = client(data_root(tmp_path), lambda request: httpx.Response(200, json=body)) \
        .post("/api/v1/routes?dummy=false", json={**REQUEST, "departure_at": "2030-01-01T12:00:00+03:00"})
    assert response.status_code == 200
    data = response.json()
    assert any("OUTSIDE_SERVICE_PERIOD" in warning for warning in data["warnings"])
    assert any("only walking options" in warning for warning in data["warnings"])
    assert data["routes"][0]["legs"][0]["mode"] == "walk"


def test_realtime_estimate_overrides_scheduled_time(tmp_path):
    body = payload()
    leg = body["data"]["planConnection"]["edges"][0]["node"]["legs"][1]
    leg["start"]["estimated"] = {"time": "2026-11-11T12:10:00+03:00", "delay": 20}
    leg["end"]["estimated"] = {"time": "2026-11-11T12:26:00+03:00", "delay": 20}
    response = client(data_root(tmp_path), lambda request: httpx.Response(200, json=body)) \
        .post("/api/v1/routes?dummy=false", json=REQUEST)
    assert response.status_code == 200
    bus = next(item for item in response.json()["routes"][0]["legs"] if item["mode"] == "bus")
    assert bus["start_at"] == "2026-11-11T12:10:00+03:00"
    assert bus["end_at"] == "2026-11-11T12:26:00+03:00"


@pytest.mark.parametrize("handler,status,code", [
    (lambda request: httpx.Response(500, text="boom"), 502, "planner_error"),
    (lambda request: httpx.Response(200, json={"errors": [{"message": "bad"}]}), 502, "planner_error"),
    (lambda request: httpx.Response(200, json={"data": {"planConnection": None}}), 502, "planner_error"),
    (lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")), 503, "planner_unavailable"),
    (lambda request: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")), 504, "planner_timeout"),
])
def test_planner_errors_are_diagnosable(tmp_path, handler, status, code):
    response = client(data_root(tmp_path), handler).post("/api/v1/routes?dummy=false", json=REQUEST)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_otp_mode_requires_explicit_real_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload())

    api = client(data_root(tmp_path), handler)
    assert api.post("/api/v1/routes", json=REQUEST).json()["mode"] == "dummy"
    assert calls == []


def test_polyline_decoder_matches_leg_endpoints():
    body = payload()
    for leg in body["data"]["planConnection"]["edges"][0]["node"]["legs"]:
        decoded = decode_polyline(leg["legGeometry"]["points"])
        assert decoded
        # OTP snaps street geometry to the network, so the line may end a few
        # metres away from the requested coordinate.
        assert abs(decoded[0][1] - leg["from"]["lat"]) < 1e-3
        assert abs(decoded[0][0] - leg["from"]["lon"]) < 1e-3
        assert abs(decoded[-1][1] - leg["to"]["lat"]) < 1e-3
        assert abs(decoded[-1][0] - leg["to"]["lon"]) < 1e-3
        for lon, lat in decoded:
            assert -180 <= lon <= 180 and -90 <= lat <= 90
    # Canonical precision-5 example: decodes to (38.5, -120.2) in (lat, lon).
    assert decode_polyline("_p~iF~ps|U_ulLnnqC")[0] == pytest.approx((-120.2, 38.5), abs=1e-4)


def test_status_reports_graph_metadata_and_stale_gtfs(tmp_path):
    root = data_root(tmp_path, graph={"version": "v1", "gtfs_source_sha256": "b" * 64,
                                      "activated_at": None, "service_end": "2020-01-01"})
    (root / "otp" / "build-status.json").write_text(json.dumps({"status": "error", "error": "boom"}))
    status = TestClient(create_app(root, "unavailable")).get("/api/v1/data/status").json()
    assert status["graph_version"] == "v1"
    assert status["graph"]["version"] == "v1"
    assert status["graph_check"]["status"] == "error"
    assert status["state"] == "degraded"
    for warning in ("different GTFS version", "confirmed as loaded", "service period has ended",
                    "graph build failed"):
        assert any(warning in item for item in status["warnings"])


def test_status_accepts_consistent_graph(tmp_path):
    root = data_root(tmp_path)
    status = TestClient(create_app(root, "unavailable")).get("/api/v1/data/status").json()
    assert status["graph_version"] == "d58c053a487d1c7e"
    assert status["state"] == "ready"
    assert status["warnings"] == []


def test_unavailable_mode_still_returns_503(tmp_path):
    response = TestClient(create_app(data_root(tmp_path), "unavailable")) \
        .post("/api/v1/routes?dummy=false", json=REQUEST)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "planner_unavailable"
