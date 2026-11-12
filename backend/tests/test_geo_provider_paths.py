"""Exercise every non-planner provider path through HTTP without live requests."""

import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dgis import BY_ID_URL, DgisAddresses, DgisRouter
from app.main import create_app
from app.yandex import YandexAddresses
from app.yandex_router import YandexRouter

PROVIDERS = ["yandex", "2gis"]
PATHS = ["suggest", "geocode-q", "geocode-uri", "reverse", "road-route"]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbid(*args, **kwargs):
        raise AssertionError("Provider path tests must never spend live quota")
    monkeypatch.setattr(socket.socket, "connect", forbid)
    for key in ("YANDEX_GEOCODER_API_KEY", "YANDEX_SUGGEST_API_KEY", "YANDEX_ROUTER_API_KEY", "DGIS_API_KEY"):
        monkeypatch.setenv(key, "offline-provider-key")


def saved_case(provider, operation):
    directory, filename = ("yandex", "live-2026-11-11.json") if provider == "yandex" else ("dgis", "live-example-2026-11-11.json")
    report = json.loads((Path(__file__).parents[1] / "app" / "fixtures" / directory / filename).read_text())
    name = "geocode" if operation.startswith("geocode-") else operation
    case = report["cases"][name]
    request = {**case["request"], "params": {**case["request"]["params"], "provider": provider, "dummy": False}}
    if operation == "geocode-q":
        request["params"].pop("uri", None)
        request["params"]["q"] = "Санкт-Петербург, Кронверкский проспект, 49"
    elif operation == "geocode-uri":
        request["params"].pop("q", None)
        request["params"]["uri"] = report["cases"]["suggest"]["api_response"]["items"][0]["uri"]
    return case, request


def client_for(tmp_path, transport):
    return TestClient(create_app(tmp_path,
        address_provider=YandexAddresses(transport), road_provider=YandexRouter(transport),
        dgis_address_provider=DgisAddresses(transport), dgis_road_provider=DgisRouter(transport)))


def call(client, request):
    return client.request(request["method"], request["path"], params=request["params"], json=request["body"])


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("operation", PATHS)
def test_real_provider_paths_return_normalized_results_in_one_call(tmp_path, provider, operation):
    case, request = saved_case(provider, operation)
    calls = []
    def handler(upstream):
        calls.append(upstream)
        assert ("2gis.com" in upstream.url.host) == (provider == "2gis")
        if operation == "geocode-uri":
            if provider == "2gis":
                assert str(upstream.url).split("?")[0] == BY_ID_URL
                assert upstream.url.params["id"] == request["params"]["uri"][5:]
            else:
                assert upstream.url.params["uri"] == request["params"]["uri"]
        return httpx.Response(200, json=case["upstream_response"])
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport, client_for(tmp_path, transport) as client:
        response = call(client, request)
    assert response.status_code == 200 and len(calls) == 1
    result = response.json()
    assert result["mode"] == result["provider"] == provider
    expected = case["api_response"]
    if operation == "road-route":
        assert result["route"] == expected["route"]
        assert result["traffic_type"] == expected["traffic_type"]
    else:
        assert result["items"] == expected["items"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("operation", PATHS)
@pytest.mark.parametrize("upstream,expected", [(408, 504), (504, 504), (429, 503)])
def test_failures_never_retry_fall_back_or_return_dummy(tmp_path, provider, operation, upstream, expected):
    _, request = saved_case(provider, operation)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(upstream, text="secret provider error containing offline-provider-key")
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport, client_for(tmp_path, transport) as client:
        response = call(client, request)
    assert response.status_code == expected and len(calls) == 1
    result = response.json()
    assert "error" in result and "mode" not in result
    assert "offline-provider-key" not in response.text
    assert result["error"]["code"].endswith("quota" if upstream == 429 else "timeout")
