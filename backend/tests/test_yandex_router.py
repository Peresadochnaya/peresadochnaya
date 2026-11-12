"""Router documentation fixtures; all real networking is forbidden."""
import copy
import json
import logging
import socket
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas import RoadRouteRequest
from app.yandex_router import ROUTER_URL, YandexRouter, driving_params


REQUEST = {"points": [{"lat": 60, "lon": 30}, {"lat": 60.02, "lon": 30.03}], "traffic_mode": "on"}
FUTURE = "2099-11-11T14:00:00+03:00"
NOW = datetime(2099, 11, 11, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("YANDEX_ROUTER_API_KEY", "fake-router-key")
    def forbidden(*args, **kwargs):
        raise AssertionError("Router tests must never spend API quota")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def route_fixture(traffic="realtime"):
    return {"traffic_type": traffic, "route": {"legs": [{"status": "OK", "steps": [
        {"duration": 10.25, "length": 100.5, "mode": "driving", "waiting_duration": 4,
         "polyline": {"points": [[60, 30], [60.01, 30.02]]}},
        {"duration": 20.5, "length": 200.25, "mode": "driving",
         "polyline": {"points": [[60.01, 30.02], [60.02, 30.03]]}},
    ]}]}}


def client(tmp_path, handler):
    provider = YandexRouter(httpx.Client(transport=httpx.MockTransport(handler)))
    return TestClient(create_app(tmp_path, road_provider=provider))


@pytest.mark.parametrize("traffic,departure,expected", [
    ("on", None, {}),
    ("on", FUTURE, {"departure_time": 4098078000}),
    ("off", FUTURE, {"traffic": "disabled"}),
    ("off", "2000-01-01T00:00:00Z", {"traffic": "disabled"}),
])
def test_documented_params_order_time_and_one_request(tmp_path, traffic, departure, expected):
    body = {**REQUEST, "traffic_mode": traffic, "departure_at": departure}
    calls = []
    # Compute the expected UNIX timestamp from a known timezone-aware instant.
    if "departure_time" in expected:
        expected = {"departure_time": int(datetime.fromisoformat(FUTURE).timestamp())}
    def handler(request):
        calls.append(request)
        assert str(request.url).split("?")[0] == ROUTER_URL
        assert dict(request.url.params) == {"waypoints": "60.0,30.0|60.02,30.03", "mode": "driving",
            "apikey": "fake-router-key", **{k: str(v) for k, v in expected.items()}}
        return httpx.Response(200, json=route_fixture("disabled" if traffic == "off" else "forecast"))
    response = client(tmp_path, handler).post("/api/v1/road-route?dummy=false", json=body)
    assert response.status_code == 200 and len(calls) == 1
    data = response.json()
    assert data["mode"] == "yandex" and data["traffic_mode"] == traffic
    assert data["route"]["distance_meters"] == 300.75
    assert data["route"]["duration_seconds"] == 30.75  # deprecated waiting_duration not added
    assert data["route"]["geometry"] == {"type": "LineString", "coordinates": [[30, 60], [30.02, 60.01], [30.03, 60.02]]}
    assert data["warnings"] and "bus" in data["warnings"][0]
    if traffic == "off":
        assert data["departure_at"] is None


def test_departure_timestamp_uses_timezone_and_rounds_up_to_seconds():
    body = RoadRouteRequest(**{**REQUEST, "departure_at": "2099-11-11T14:00:00.1+03:00"})
    assert driving_params(body, NOW)["departure_time"] == int(datetime(2099, 11, 11, 11, tzinfo=timezone.utc).timestamp()) + 1


@pytest.mark.parametrize("suffix", ["", "?dummy=true"])
def test_dummy_never_calls_yandex_even_with_key(tmp_path, suffix):
    def handler(request):
        pytest.fail("Dummy mode reached provider")
    body = {**REQUEST, "departure_at": "2000-01-01T00:00:00Z"}
    data = client(tmp_path, handler).post("/api/v1/road-route" + suffix, json=body).json()
    assert data["mode"] == "dummy" and data["traffic_type"] is None
    assert data["warnings"] and data["route"]["distance_meters"] > 0
    assert data["route"]["geometry"]["coordinates"] == [[30, 60], [30.03, 60.02]]


def test_identical_dummy_endpoints_have_zero_distance_and_time(tmp_path):
    body = {"points": [REQUEST["points"][0]] * 2}
    data = client(tmp_path, lambda r: pytest.fail("Unexpected provider call")).post("/api/v1/road-route", json=body).json()
    assert data["route"]["distance_meters"] == data["route"]["duration_seconds"] == 0


def test_failed_leg_is_no_route_not_partial_or_error(tmp_path):
    response = client(tmp_path, lambda r: httpx.Response(200, json={"traffic_type": "realtime",
        "route": {"legs": [{"status": "FAIL"}]}})).post("/api/v1/road-route?dummy=false", json=REQUEST)
    assert response.status_code == 200
    assert response.json()["route"] is None and response.json()["warnings"]


@pytest.mark.parametrize("mutation", [
    lambda d: d.pop("route"),
    lambda d: d.update(traffic_type="unknown"),
    lambda d: d.update(traffic_type="disabled"),
    lambda d: d["route"].update(legs=[]),
    lambda d: d["route"]["legs"][0].update(status="UNKNOWN"),
    lambda d: d["route"]["legs"][0].update(steps=[]),
    lambda d: d["route"]["legs"][0]["steps"][0].update(duration=-1),
    lambda d: d["route"]["legs"][0]["steps"][0].update(length=True),
    lambda d: d["route"]["legs"][0]["steps"][0].update(duration=float("nan")),
    lambda d: d["route"]["legs"][0]["steps"][0].update(duration="10"),
    lambda d: d["route"]["legs"][0]["steps"][0].update(mode="transit"),
    lambda d: d["route"]["legs"][0]["steps"][0].update(polyline={"points": [[95, 30], [60, 30]]}),
    lambda d: d["route"]["legs"][0]["steps"][0].update(polyline={"points": [[60, 30, 1], [60, 30]]}),
    lambda d: d["route"]["legs"][0]["steps"][0].update(polyline={"points": [[60, 30]]}),
    lambda d: d["route"]["legs"][0]["steps"][1].update(polyline={"points": [[61, 30], [61, 31]]}),
])
def test_invalid_upstream_never_returns_fake_or_partial_result(tmp_path, mutation):
    payload = copy.deepcopy(route_fixture())
    mutation(payload)
    response = client(tmp_path, lambda r: httpx.Response(200, content=json.dumps(payload))).post(
        "/api/v1/road-route?dummy=false", json=REQUEST)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "router_provider_error"


@pytest.mark.parametrize("status,expected", [(401, 502), (429, 503), (500, 502), (302, 502)])
def test_errors_sanitized_no_retries_or_redirects(tmp_path, status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"errors": ["fake-router-key confidential"]},
                              headers={"location": "https://example.com/redirect"})
    response = client(tmp_path, handler).post("/api/v1/road-route?dummy=false", json=REQUEST)
    assert response.status_code == expected and len(calls) == 1
    assert "fake-router-key" not in response.text and "confidential" not in response.text


def test_timeout_is_safe_and_single_request(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("contains fake-router-key", request=request)
    response = client(tmp_path, handler).post("/api/v1/road-route?dummy=false", json=REQUEST)
    assert response.status_code == 504 and len(calls) == 1
    assert response.json()["error"]["code"] == "router_provider_timeout"
    assert "fake-router-key" not in response.text


def test_http_client_logs_redact_query_key(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        response = client(tmp_path, lambda r: httpx.Response(200, json=route_fixture())).post(
            "/api/v1/road-route?dummy=false", json=REQUEST)
    assert response.status_code == 200
    assert ROUTER_URL in caplog.text and "[REDACTED]" in caplog.text
    assert "fake-router-key" not in caplog.text


def test_invalid_request_past_forecast_and_missing_key_do_not_spend_quota(tmp_path, monkeypatch):
    api = client(tmp_path, lambda r: pytest.fail("Invalid input or missing key must not call Yandex"))
    for body in [
        {"points": []}, {"points": REQUEST["points"][:1]},
        {"points": REQUEST["points"] + REQUEST["points"][:1]},
        {**REQUEST, "traffic_mode": "unknown"},
        {**REQUEST, "departure_at": "2099-11-11T14:00:00"},
        {**REQUEST, "departure_at": "2000-01-01T00:00:00Z"},
        {"points": [{"lat": 91, "lon": 30}, REQUEST["points"][1]]},
    ]:
        assert api.post("/api/v1/road-route?dummy=false", json=body).status_code == 422
    assert api.post("/api/v1/road-route?dummy=typo", json=REQUEST).status_code == 422
    monkeypatch.delenv("YANDEX_ROUTER_API_KEY")
    response = api.post("/api/v1/road-route?dummy=false", json=REQUEST)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "router_provider_unavailable"
