"""OTP adapter regressions: recorded itinerary, errors and status metadata."""
import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.otp import OtpPlanner, decode_polyline, mode_reluctance
from app.schemas import FarePreferences, Leg, LineString, Passenger, Place, TransitLine

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
    assert data["mode"] == "real"
    assert data["sources"] == {"candidate_providers": ["otp"], "traffic_providers": [],
                               "realtime": "unavailable"}
    assert data["routes"][0]["provenance"] == {"candidate_providers": ["otp"],
                                               "traffic_providers": []}
    assert data["gtfs_version"] == GTFS_SHA and data["graph_version"] == "d58c053a487d1c7e"
    assert any("selected 2026 ticket profile" in warning for warning in data["warnings"])
    route = data["routes"][0]
    assert route["transfers"] == node["numberOfTransfers"]
    assert route["start_at"] == node["start"] and route["end_at"] == node["end"]
    assert route["duration_seconds"] == node["duration"]
    assert route["distance_meters"] == pytest.approx(sum(leg["distance"] for leg in node["legs"]), abs=0.01)
    assert route["name"].startswith("Автобус")
    assert route["fare"]["amount_rub"] == 176.0

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

    variables = requests[-1]["variables"]
    assert variables["first"] == 5
    assert variables["dateTime"]["earliestDeparture"] == "2026-11-11T12:00:00+03:00"
    assert [item["mode"] for item in variables["modes"]["transit"]["transit"]] == [
        "BUS", "TRAM", "TROLLEYBUS", "SUBWAY", "RAIL"]
    assert variables["preferences"] == {
        "street": {"walk": {"speed": 1.3, "reluctance": 4.0}},
        "transit": {"transfer": {}},
    }


def test_arrival_search_uses_latest_arrival(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload())

    request = {key: value for key, value in REQUEST.items() if key != "departure_at"}
    request["arrival_at"] = "2026-11-11T13:00:00+03:00"
    request["max_transfers"] = 7
    response = client(data_root(tmp_path), handler).post("/api/v1/routes?dummy=false", json=request)
    assert response.status_code == 200
    variables = requests[0]["variables"]
    assert variables["dateTime"] == {"latestArrival": "2026-11-11T13:00:00+03:00"}
    assert variables["preferences"]["transit"]["transfer"] == {"maximumTransfers": 7}


def test_search_filters_are_forwarded_to_otp(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload())

    request = {**REQUEST, "modes": ["bus", "metro", "rail"],
               "exclude_modes": ["rail"], "max_transfers": 1,
               "walk_speed": "fast", "less_walking": True}
    response = client(data_root(tmp_path), handler).post("/api/v1/routes?dummy=false", json=request)
    assert response.status_code == 200
    variables = requests[-1]["variables"]
    assert [item["mode"] for item in variables["modes"]["transit"]["transit"]] == ["BUS", "SUBWAY"]
    assert variables["preferences"] == {
        "street": {"walk": {"speed": 1.7, "reluctance": 6.0}},
        "transit": {"transfer": {"maximumTransfers": 1}},
    }


def test_all_transit_modes_can_be_excluded_for_walking_only_search(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload())

    response = client(data_root(tmp_path), handler).post(
        "/api/v1/routes?dummy=false",
        json={**REQUEST, "exclude_modes": ["bus", "tram", "trolleybus", "metro", "rail"]},
    )
    assert response.status_code == 200
    assert requests[0]["variables"]["modes"] == {"direct": ["WALK"], "directOnly": True}


def test_routes_are_sorted_by_duration(tmp_path):
    body = payload()
    fast = body["data"]["planConnection"]["edges"][0]["node"]
    slow = json.loads(json.dumps(fast))
    # Duration is the ranking field supplied by OTP; leg timing remains a
    # valid, self-consistent fixture for both alternatives.
    slow["duration"] = fast["duration"] + 60
    body["data"]["planConnection"]["edges"] = [{"node": slow}, {"node": fast}]
    response = client(data_root(tmp_path), lambda request: httpx.Response(200, json=body)) \
        .post("/api/v1/routes?dummy=false", json=REQUEST)
    assert response.status_code == 200
    durations = [route["duration_seconds"] for route in response.json()["routes"]]
    assert durations == sorted(durations)


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


FARE = {"rules": [{"mode": "metro", "cost_rub": 0}, {"mode": "bus", "cost_rub": 65}],
        "transfer_cost_rub": 20}


def test_fare_rules_become_reluctance_and_are_billed(tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload())

    response = client(data_root(tmp_path), handler) \
        .post("/api/v1/routes?dummy=false", json={**REQUEST, "fare": FARE})
    assert response.status_code == 200
    data = response.json()

    transit = requests[-1]["variables"]["modes"]["transit"]["transit"]
    by_mode = {item["mode"]: item.get("cost", {}).get("reluctance") for item in transit}
    assert by_mode["SUBWAY"] == 1.0                      # free metro is neutral
    assert by_mode["BUS"] == pytest.approx(1.433, abs=1e-3)  # 65 RUB at 600 RUB/h
    assert by_mode["TRAM"] == 1.0                        # no rule means no penalty

    # The recorded itinerary boards bus 24 and bus 100: two fares plus one transfer.
    fare = data["routes"][0]["fare"]
    assert fare["currency"] == "RUB"
    assert fare["items"] == [{"mode": "bus", "rides": 2, "cost_rub": 130.0}]
    assert fare["transfers"] == 1
    assert fare["transfer_cost_rub"] == 20.0
    assert fare["amount_rub"] == 150.0

    assert any("explicit tariff rules" in warning for warning in data["warnings"])
    assert any("time multipliers" in warning for warning in data["warnings"])


def test_unpriced_mode_is_reported_as_free(tmp_path):
    fare = {"rules": [{"mode": "metro", "cost_rub": 0}]}
    response = client(data_root(tmp_path), lambda request: httpx.Response(200, json=payload())) \
        .post("/api/v1/routes?dummy=false", json={**REQUEST, "fare": fare})
    assert response.status_code == 200
    data = response.json()
    assert data["routes"][0]["fare"]["amount_rub"] == 176.0


def test_fare_requires_unique_modes(tmp_path):
    fare = {"rules": [{"mode": "bus", "cost_rub": 65}, {"mode": "bus", "cost_rub": 40}]}
    response = client(data_root(tmp_path), lambda request: httpx.Response(200, json=payload())) \
        .post("/api/v1/routes?dummy=false", json={**REQUEST, "fare": fare})
    assert response.status_code == 422


def test_dummy_route_does_not_claim_a_calculated_fare(tmp_path):
    response = TestClient(create_app(data_root(tmp_path), "dummy")) \
        .post("/api/v1/routes", json={**REQUEST, "fare": FARE})
    assert response.status_code == 200
    data = response.json()
    assert data["routes"][0]["fare"] is None
    assert any("Fare rules are ignored" in warning for warning in data["warnings"])


def test_reluctance_is_capped_and_neutral_for_free_modes():
    assert mode_reluctance(0, 600) == 1.0
    assert mode_reluctance(600, 600) == 5.0
    assert mode_reluctance(100000, 600) == 20.0


def test_route_fare_skips_walking_and_waiting_legs():
    point = Place(lat=59.93, lon=30.33, name="A")
    line = LineString(coordinates=[(30.33, 59.93), (30.34, 59.94)])
    legs = [Leg(mode="walk", origin=point, destination=point,
                start_at="2026-11-11T12:00:00+03:00", end_at="2026-11-11T12:05:00+03:00",
                duration_seconds=300, original_duration_seconds=300,
                duration_source="schedule", distance_meters=400.0, geometry=line),
            Leg(mode="metro", origin=point, destination=point,
                start_at="2026-11-11T12:05:00+03:00", end_at="2026-11-11T12:15:00+03:00",
                duration_seconds=600, original_duration_seconds=600,
                duration_source="schedule", distance_meters=5000.0,
                line=TransitLine(short_name="М1", route_id="m1"), geometry=line),
            Leg(mode="bus", origin=point, destination=point,
                start_at="2026-11-11T12:15:00+03:00", end_at="2026-11-11T12:35:00+03:00",
                duration_seconds=1200, original_duration_seconds=1200,
                duration_source="schedule", distance_meters=8000.0,
                line=TransitLine(short_name="24", route_id="b24"), geometry=line)]
    from app.otp import route_fare
    fare = route_fare(legs, FarePreferences(**FARE))
    assert [item.model_dump() for item in fare.items] == [
        {"mode": "bus", "rides": 1, "cost_rub": 65.0},
        {"mode": "metro", "rides": 1, "cost_rub": 0.0}]
    assert fare.transfers == 1
    assert fare.amount_rub == 85.0


def test_route_fare_counts_metro_line_change_as_one_ride():
    point = Place(lat=59.93, lon=30.33, name="A")
    line = LineString(coordinates=[(30.33, 59.93), (30.34, 59.94)])

    def ride(mode, start, end, route_id=None):
        return Leg(mode=mode, origin=point, destination=point, start_at=start, end_at=end,
                   duration_seconds=300, original_duration_seconds=300,
                   duration_source="schedule", distance_meters=0.0 if mode == "wait" else 1000.0,
                   line=TransitLine(short_name=route_id, route_id=route_id) if route_id else None,
                   geometry=line)

    # A line change inside the paid area: walk → wait → metro 1 → wait → metro 2 → walk.
    legs = [
        ride("walk", "2026-11-13T06:59:27+03:00", "2026-11-13T07:07:32+03:00"),
        ride("wait", "2026-11-13T07:07:32+03:00", "2026-11-13T07:12:25+03:00"),
        ride("metro", "2026-11-13T07:12:25+03:00", "2026-11-13T07:18:25+03:00", "1:metro:1"),
        ride("wait", "2026-11-13T07:18:25+03:00", "2026-11-13T07:32:08+03:00"),
        ride("metro", "2026-11-13T07:32:08+03:00", "2026-11-13T07:36:38+03:00", "1:metro:2"),
        ride("walk", "2026-11-13T07:36:38+03:00", "2026-11-13T07:44:47+03:00"),
    ]
    from app.otp import route_fare
    bank = route_fare(legs, None, Passenger(ticket_profile="bank_card"))
    assert [item.model_dump() for item in bank.items] == [
        {"mode": "metro", "rides": 1, "cost_rub": 95.0}]
    assert bank.amount_rub == 95.0
    podorozhnik = route_fare(legs, None, Passenger(ticket_profile="podorozhnik_wallet"))
    assert [item.model_dump() for item in podorozhnik.items] == [
        {"mode": "metro", "rides": 1, "cost_rub": 65.0}]
    assert podorozhnik.amount_rub == 65.0


def test_route_fare_keeps_separate_metro_rides_after_surface_leg():
    point = Place(lat=59.93, lon=30.33, name="A")
    line = LineString(coordinates=[(30.33, 59.93), (30.34, 59.94)])

    def ride(mode, start, end, route_id):
        return Leg(mode=mode, origin=point, destination=point, start_at=start, end_at=end,
                   duration_seconds=300, original_duration_seconds=300,
                   duration_source="schedule", distance_meters=1000.0,
                   line=TransitLine(short_name=route_id, route_id=route_id), geometry=line)

    legs = [
        ride("metro", "2026-11-13T12:00:00+03:00", "2026-11-13T12:10:00+03:00", "1:metro:1"),
        ride("bus", "2026-11-13T12:20:00+03:00", "2026-11-13T12:30:00+03:00", "2:bus:1"),
        ride("metro", "2026-11-13T12:40:00+03:00", "2026-11-13T12:50:00+03:00", "1:metro:2"),
    ]
    from app.otp import route_fare
    fare = route_fare(legs, None, Passenger(ticket_profile="bank_card"))
    assert {item.mode: item.rides for item in fare.items} == {"metro": 2, "bus": 1}
    assert fare.amount_rub == 278.0


def test_podorozhnik_surface_transfer_chain_and_metro_reset():
    point = Place(lat=59.93, lon=30.33, name="A")
    line = LineString(coordinates=[(30.33, 59.93), (30.34, 59.94)])

    def ride(mode, start, end, route_id):
        return Leg(mode=mode, origin=point, destination=point, start_at=start, end_at=end,
                   duration_seconds=300, original_duration_seconds=300,
                   duration_source="schedule", distance_meters=1000.0,
                   line=TransitLine(short_name=route_id, route_id=route_id), geometry=line)

    legs = [
        ride("bus", "2026-11-11T12:00:00+03:00", "2026-11-11T12:05:00+03:00", "b1"),
        ride("tram", "2026-11-11T12:20:00+03:00", "2026-11-11T12:25:00+03:00", "t1"),
        ride("trolleybus", "2026-11-11T12:40:00+03:00", "2026-11-11T12:45:00+03:00", "tr1"),
        ride("metro", "2026-11-11T12:50:00+03:00", "2026-11-11T12:55:00+03:00", "m1"),
        ride("bus", "2026-11-11T13:00:00+03:00", "2026-11-11T13:05:00+03:00", "b2"),
    ]
    from app.otp import route_fare
    fare = route_fare(legs, None, Passenger(ticket_profile="podorozhnik_wallet"))
    assert {item.mode: item.cost_rub for item in fare.items} == {
        "bus": 130.0, "tram": 14.0, "trolleybus": 0.0, "metro": 65.0,
    }
    assert fare.amount_rub == 209.0
