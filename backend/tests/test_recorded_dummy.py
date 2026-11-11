"""Regressions using the four saved live calls; no network or real keys."""
import json
import socket
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.recorded_dummy import DUMMY_URI_PREFIX, FIXTURE_PATH, recorded_samples
from app.yandex import YandexAddresses, YandexClient
from app.yandex_router import YandexRouter


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded-fixture tests must not access network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.mark.parametrize("name", ["suggest", "geocode", "reverse", "road-route"])
def test_original_live_response_still_normalizes_offline(tmp_path, monkeypatch, name):
    monkeypatch.setenv("YANDEX_GEOCODER_API_KEY", "fixture-only-key")
    monkeypatch.setenv("YANDEX_SUGGEST_API_KEY", "fixture-only-key")
    monkeypatch.setenv("YANDEX_ROUTER_API_KEY", "fixture-only-key")
    case = recorded_samples()["cases"][name]
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url.copy_remove_param("apikey")) == case["upstream_request"]["url"]
        return httpx.Response(case["upstream_status"], json=case["upstream_response"])
    with httpx.Client(transport=httpx.MockTransport(handler)) as provider:
        client = TestClient(create_app(tmp_path, address_provider=YandexAddresses(provider),
                                      road_provider=YandexRouter(provider)))
        req = case["request"]
        response = client.request(req["method"], req["path"], params=req["params"], json=req["body"])
    assert response.status_code == 200 and len(calls) == 1
    actual, expected = response.json(), dict(case["api_response"])
    expected["provider"] = "yandex"
    if name == "road-route":
        actual.pop("calculated_at")
        expected.pop("calculated_at")
    assert actual == expected


def test_recorded_suggest_geocode_flow_is_dummy_and_never_uses_real_uris(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Dummy must not reach provider")
    monkeypatch.setattr(YandexClient, "_get", forbidden)
    client = TestClient(create_app(tmp_path))
    response = client.get("/api/v1/suggest", params={"q": " пионерская ", "limit": 3})
    data = response.json()
    assert data["mode"] == "dummy" and len(data["items"]) == 3
    assert data["items"][0]["title"] == "Пионерская улица"
    assert all(item["uri"].startswith(DUMMY_URI_PREFIX) for item in data["items"])
    assert any("Recorded" in warning for warning in data["warnings"])
    uri = data["items"][0]["uri"]
    place = client.get("/api/v1/geocode", params={"uri": uri}).json()
    assert place["mode"] == "dummy"
    assert place["items"] == recorded_samples()["cases"]["geocode"]["api_response"]["items"]
    assert client.get("/api/v1/geocode", params={"uri": uri, "dummy": False}).status_code == 422
    # Do not assign the first suggestion's coordinates to another city.
    other = client.get("/api/v1/geocode", params={"uri": data["items"][1]["uri"]}).json()
    assert not any("Recorded" in warning for warning in other["warnings"])
    assert len(client.get("/api/v1/suggest", params={"q": "Пионерская", "limit": 1}).json()["items"]) == 1
    # A truncated response must not mutate the cached original fixture.
    assert len(client.get("/api/v1/suggest", params={"q": "Пионерская", "limit": 3}).json()["items"]) == 3


def test_recorded_geocode_text_reverse_and_route_replay_keep_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(YandexClient, "_get", lambda *a, **kw: pytest.fail("No provider calls in replay"))
    client = TestClient(create_app(tmp_path))
    cases = recorded_samples()["cases"]
    geocode = client.get("/api/v1/geocode", params={"q": "Пионерская улица"}).json()
    assert geocode["items"] == cases["geocode"]["api_response"]["items"]
    reverse = client.get("/api/v1/reverse", params={"lat": 59.956433, "lon": 30.310138}).json()
    assert reverse["mode"] == "dummy" and reverse["items"] == cases["reverse"]["api_response"]["items"]
    response = client.post("/api/v1/road-route", json=cases["road-route"]["request"]["body"])
    data = response.json()
    assert data["route"] == cases["road-route"]["api_response"]["route"]
    assert data["mode"] == "dummy" and data["traffic_type"] is None
    assert data["calculated_at"] == cases["road-route"]["api_response"]["calculated_at"]
    assert any("not a current lookup" in warning for warning in data["warnings"])
    assert datetime.fromisoformat(data["calculated_at"]).tzinfo is not None


def test_other_inputs_and_traffic_mode_do_not_reuse_unrelated_live_geometry(tmp_path):
    client = TestClient(create_app(tmp_path))
    body = dict(recorded_samples()["cases"]["road-route"]["request"]["body"])
    body["traffic_mode"] = "off"
    data = client.post("/api/v1/road-route", json=body).json()
    assert len(data["route"]["geometry"]["coordinates"]) == 2
    assert not any("Recorded" in warning for warning in data["warnings"])
    other = client.get("/api/v1/suggest", params={"q": "Ручьи"}).json()
    assert other["items"][0]["uri"].startswith("dummy:yandex:stop:")


def test_saved_call_ledger_and_fixture_have_no_credential_parameters():
    data = json.loads(FIXTURE_PATH.read_text())
    assert data["counts"] == {"suggest": 1, "geocoder": 2, "router": 1}
    assert len(data["cases"]) == 4
    for case in data["cases"].values():
        assert case["api_status"] == case["upstream_status"] == 200
        assert "apikey=" not in case["upstream_request"]["url"]
