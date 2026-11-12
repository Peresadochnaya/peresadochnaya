"""OTP adapter regressions: recorded itinerary, errors and status metadata."""
import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.otp import OtpPlanner, decode_polyline, mode_reluctance
from app.schemas import FarePreferences, Leg, LineString, Place, TransitLine

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


@pytest.mark.parametrize("options", [
    {"providers": ["yandex"]}, {"providers": ["otp", "2gis"]},
    {"providers": ["2gis"]}, {"max_transfers": 0}, {"max_transfers": 2},
    {"walk_speed": "slow"}, {"walk_speed": "fast"}, {"less_walking": True},
    {"modes": ["bus"], "exclude_modes": ["bus"]},
])
def test_unsupported_real_options_do_not_contact_otp(tmp_path, options):
    calls = []
    response = client(data_root(tmp_path), lambda request: calls.append(request)) \
        .post("/api/v1/routes?dummy=false", json={**REQUEST, **options})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_planner_options"
    assert calls == []


def test_mode_filter_and_current_provenance_contract(tmp_path):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=payload())
    response = client(data_root(tmp_path), handler).post("/api/v1/routes?dummy=false", json={
        **REQUEST, "modes": ["bus", "metro", "rail"], "exclude_modes": ["rail"],
        "traffic_mode": "on", "passenger": {"ticket_profile": "bank_card"}})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "real"
    assert data["sources"] == {"candidate_providers": ["otp"], "traffic_providers": [], "realtime": "unavailable"}
    assert data["passenger"]["ticket_profile"] == "bank_card"
    assert any("traffic_mode is not applied" in item for item in data["warnings"])
    assert calls[0]["variables"]["modes"]["transit"]["transit"] == [{"mode": "BUS"}, {"mode": "SUBWAY"}]
    for route in data["routes"]:
        assert route["provenance"] == {"candidate_providers": ["otp"], "traffic_providers": []}
        assert route["distance_meters"] == pytest.approx(sum(leg["distance_meters"] for leg in route["legs"]), abs=.01)
        assert all(leg["distance_meters"] == 0 for leg in route["legs"] if leg["mode"] == "wait")


def test_default_dummy_preserves_current_coherent_demo(tmp_path):
    calls = []
    api = client(data_root(tmp_path), lambda request: calls.append(request))
    baseline = TestClient(create_app(tmp_path, "unavailable"))
    options = {**REQUEST, "walk_speed": "slow", "less_walking": True, "max_transfers": 0}
    otp_demo = api.post("/api/v1/routes", json=options).json()
    old_demo = baseline.post("/api/v1/routes", json=options).json()
    otp_demo.pop("calculated_at")
    old_demo.pop("calculated_at")
    assert otp_demo == old_demo
    assert otp_demo["mode"] == "dummy"
    assert calls == []


def test_otp_url_and_timeout_from_environment(monkeypatch):
    monkeypatch.setenv("OTP_URL", "http://otp:8080/")
    monkeypatch.setenv("OTP_TIMEOUT_SECONDS", "7")
    planner = OtpPlanner()
    assert planner.url == "http://otp:8080"
    assert planner.timeout == 7



def test_otp_uses_loaded_graph_when_source_calendar_has_no_services(tmp_path):
    root = data_root(tmp_path)
    current = root / "gtfs" / "current.json"
    source = json.loads(current.read_text())
    source["active_service_ids"] = 0
    current.write_text(json.dumps(source))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload())
    response = client(root, handler).post("/api/v1/routes?dummy=false", json=REQUEST)
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "real"
    assert len(calls) == 1
    assert data["gtfs_version"] == GTFS_SHA
    assert any("Source GTFS validation reported no active services" in warning
               and "Source validation remains unsuccessful" in warning for warning in data["warnings"])
    # Existing non-OTP configurations retain the source-calendar guard.
    response = TestClient(create_app(root, "unavailable")).post(
        "/api/v1/routes?dummy=false", json=REQUEST)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_service_date"


def test_real_response_reports_graph_feed_instead_of_newer_download(tmp_path):
    root = data_root(tmp_path, graph={"version":"graph-old", "gtfs_source_sha256":"b"*64,
        "gtfs_calendar_fixed":True, "metro_source_sha256":"c"*64})
    response = client(root, lambda request: httpx.Response(200,json=payload())).post(
        "/api/v1/routes?dummy=false",json=REQUEST)
    assert response.status_code == 200
    data=response.json()
    assert data["gtfs_version"] == "b"*64
    assert data["graph_version"] == "graph-old"
    assert any(w.startswith("graph_uses_previous_gtfs") for w in data["warnings"])
    assert any(w.startswith("graph_calendar_repaired") for w in data["warnings"])
    assert any(w.startswith("metro_times_estimated") for w in data["warnings"])


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

    transit = requests[0]["variables"]["modes"]["transit"]["transit"]
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
    assert any("No fare rule" in warning and "bus" in warning for warning in data["warnings"])
    assert data["routes"][0]["fare"]["amount_rub"] == 0.0


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
    assert any("fare, eligibility and pass coverage are not calculated" in warning for warning in data["warnings"])


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


def test_graph_with_modelled_metro_marks_ride_and_wait_estimated(tmp_path):
    body=payload()
    node=body["data"]["planConnection"]["edges"][0]["node"]
    transit=next(leg for leg in node["legs"] if leg.get("mode") == "BUS")
    transit["mode"]="SUBWAY"
    root=data_root(tmp_path,graph={"version":"metro-model","gtfs_source_sha256":GTFS_SHA,"metro_source_sha256":"c"*64})
    data=client(root,lambda request:httpx.Response(200,json=body)).post("/api/v1/routes?dummy=false",json=REQUEST).json()
    legs=data["routes"][0]["legs"]
    assert any(leg["mode"]=="metro" for leg in legs)
    for index,leg in enumerate(legs):
        if leg["mode"]=="metro" or (leg["mode"]=="wait" and index+1<len(legs) and legs[index+1]["mode"]=="metro"):
            assert leg["duration_source"]=="estimated"


def test_otp_feed_namespace_is_removed_for_gtfs_detail_endpoints(tmp_path):
    response=client(data_root(tmp_path),lambda request:httpx.Response(200,json=payload())).post(
        "/api/v1/routes?dummy=false",json=REQUEST)
    assert response.status_code==200
    legs=response.json()["routes"][0]["legs"]
    transit=next(leg for leg in legs if leg["mode"]=="bus")
    assert transit["line"]["route_id"]=="229"
    assert transit["line"]["trip_id"]=="79658598"
    assert transit["from"]["stop_id"]=="36758"
    assert not any((place["stop_id"] or "").startswith("1:") for leg in legs for place in (leg["from"],leg["to"]))


def test_native_metro_colons_survive_removing_only_otp_namespace(tmp_path):
    body=payload()
    raw=next(leg for leg in body["data"]["planConnection"]["edges"][0]["node"]["legs"] if leg.get("mode")=="BUS")
    raw["route"]["gtfsId"]="1:metro:2"
    raw["trip"]["gtfsId"]="1:metro:2:1:weekday"
    raw["from"]["stop"]["gtfsId"]="1:metro:mop-par"
    data=client(data_root(tmp_path),lambda request:httpx.Response(200,json=body)).post(
        "/api/v1/routes?dummy=false",json=REQUEST).json()
    transit=next(leg for leg in data["routes"][0]["legs"] if leg["line"] and leg["line"]["route_id"]=="metro:2")
    assert transit["line"]["trip_id"]=="metro:2:1:weekday"
    assert transit["from"]["stop_id"]=="metro:mop-par"
