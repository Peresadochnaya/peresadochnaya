"""Documentation-shaped fixtures only. Never call a live Yandex endpoint."""
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.yandex import GEOCODER_URL, SEARCH_BBOX, SUGGEST_URL, YandexAddresses


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not spend API quota")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("YANDEX_GEOCODER_API_KEY", "fake-geocoder-key")
    monkeypatch.setenv("YANDEX_SUGGEST_API_KEY", "fake-suggest-key")


def fixture_geocode(pos="30.310138 59.956433"):
    return {"response": {"GeoObjectCollection": {"featureMember": [{"GeoObject": {
        "Point": {"pos": pos}, "metaDataProperty": {"GeocoderMetaData": {
            "text": "Санкт-Петербург, Кронверкский проспект", "precision": "street"}}}}]}}}


def api(tmp_path, handler):
    transport = httpx.MockTransport(handler)
    provider = YandexAddresses(httpx.Client(transport=transport))
    return TestClient(create_app(tmp_path, "unavailable", address_provider=provider))


def test_suggest_request_and_response_follow_documentation(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url).split("?")[0] == SUGGEST_URL
        assert dict(request.url.params) == {
            "text": "Ручьи", "results": "2", "lang": "ru", "attrs": "uri",
            "highlight": "0", "bbox": SEARCH_BBOX, "countries": "ru", "types": "geo",
            "print_address": "1", "apikey": "fake-suggest-key"}
        return httpx.Response(200, json={"results": [
            {"title": {"text": "Ручьи"}, "subtitle": {"text": "Санкт-Петербург"},
             "uri": "ymapsbm1://geo?data=fixture"},
            {"title": {"text": "Ручьи, станция"}, "uri": "ymapsbm1://geo?data=fixture2"}]})
    response = api(tmp_path, handler).get("/api/v1/suggest", params={"q": " Ручьи ", "limit": 2, "dummy": False})
    assert response.status_code == 200 and len(calls) == 1
    data = response.json()
    assert data["mode"] == "yandex" and data["warnings"] == []
    assert data["items"][0] == {"title": "Ручьи", "subtitle": "Санкт-Петербург", "uri": "ymapsbm1://geo?data=fixture"}
    assert data["items"][1]["subtitle"] is None


@pytest.mark.parametrize("query, expected", [
    ({"q": "Кронверкский проспект"}, {"geocode": "Кронверкский проспект", "bbox": SEARCH_BBOX, "rspn": "0"}),
    ({"uri": "ymapsbm1://geo?data=some+opaque&value=1"}, {"uri": "ymapsbm1://geo?data=some+opaque&value=1"}),
])
def test_geocode_address_or_uri_request(tmp_path, query, expected):
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url).split("?")[0] == GEOCODER_URL
        assert dict(request.url.params) == {"format": "json", "lang": "ru_RU", "results": "1",
                                          "apikey": "fake-geocoder-key", **expected}
        return httpx.Response(200, json=fixture_geocode())
    response = api(tmp_path, handler).get("/api/v1/geocode", params={**query, "dummy": False})
    assert response.status_code == 200 and len(calls) == 1
    item = response.json()["items"][0]
    assert item["lon"] == 30.310138 and item["lat"] == 59.956433
    assert item["precision"] == "street" and item["label"].startswith("Санкт-Петербург")


def test_reverse_preserves_longitude_latitude_order(tmp_path):
    def handler(request):
        assert request.url.params["geocode"] == "30.0,60.0"
        assert request.url.params["sco"] == "longlat"
        assert "bbox" not in request.url.params and "uri" not in request.url.params
        return httpx.Response(200, json=fixture_geocode())
    response = api(tmp_path, handler).get("/api/v1/reverse", params={"lat": 60, "lon": 30, "dummy": False})
    assert response.status_code == 200 and response.json()["mode"] == "yandex"


@pytest.mark.parametrize("path,params,payload", [
    ("suggest", {"q": "ничего"}, {}),
    ("geocode", {"q": "ничего"}, {"response": {"GeoObjectCollection": {"featureMember": []}}}),
])
def test_empty_results_are_success(tmp_path, path, params, payload):
    client = api(tmp_path, lambda r: httpx.Response(200, json=payload))
    response = client.get("/api/v1/" + path, params={**params, "dummy": False})
    assert response.status_code == 200 and response.json()["items"] == []


@pytest.mark.parametrize("upstream,expected", [(400, 502), (403, 502), (429, 503), (500, 502), (302, 502)])
def test_upstream_errors_are_sanitized_and_not_retried(tmp_path, upstream, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(upstream, json={"error": "private upstream detail"},
                              headers={"location": "https://example.com/redirect"})
    response = api(tmp_path, handler).get("/api/v1/suggest", params={"q": "Ручьи", "dummy": False})
    assert response.status_code == expected and len(calls) == 1
    assert "fake-suggest-key" not in response.text and "private upstream detail" not in response.text


def test_timeout_is_not_retried(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive URL with key", request=request)
    response = api(tmp_path, handler).get("/api/v1/geocode", params={"q": "Ручьи", "dummy": False})
    assert response.status_code == 504 and len(calls) == 1
    assert "sensitive" not in response.text


@pytest.mark.parametrize("payload", [[], {"results": None}, {"results": [{}]}, {"results": [{"title": {"text": "bad"}}]}])
def test_malformed_suggest_is_provider_error(tmp_path, payload):
    response = api(tmp_path, lambda r: httpx.Response(200, json=payload)).get(
        "/api/v1/suggest", params={"q": "Ручьи", "dummy": False})
    assert response.status_code == 502


@pytest.mark.parametrize("payload", [{}, fixture_geocode("NaN 60"), fixture_geocode("30 95"), fixture_geocode("wrong")])
def test_malformed_geocoder_is_provider_error(tmp_path, payload):
    response = api(tmp_path, lambda r: httpx.Response(200, json=payload)).get(
        "/api/v1/geocode", params={"q": "Ручьи", "dummy": False})
    assert response.status_code == 502


def test_dummy_and_invalid_inputs_never_reach_provider(tmp_path):
    def handler(request):
        raise AssertionError("Provider must not be called")
    client = api(tmp_path, handler)
    for path, params in [("suggest", {"q": "Ручьи"}), ("geocode", {"q": "Ручьи"}),
                         ("geocode", {"uri": "dummy:suggestion:1"}), ("reverse", {"lat": 60, "lon": 30})]:
        response = client.get("/api/v1/" + path, params=params)
        assert response.status_code == 200
        assert response.json()["mode"] == "dummy" and response.json()["warnings"]
    for params in [{}, {"q": "Ручьи", "uri": "ymapsbm1://geo?data=x"}, {"uri": "dummy:suggestion:1"},
                   {"uri": "http://localhost/private"}, {"q": "  "}]:
        assert client.get("/api/v1/geocode", params={**params, "dummy": False}).status_code == 422
    assert client.get("/api/v1/reverse", params={"lat": 91, "lon": 30, "dummy": False}).status_code == 422


def test_missing_keys_do_not_attempt_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("YANDEX_GEOCODER_API_KEY")
    monkeypatch.delenv("YANDEX_SUGGEST_API_KEY")
    client = api(tmp_path, lambda r: pytest.fail("Missing key must not spend a request"))
    for path, params in [("suggest", {"q": "Ручьи"}), ("geocode", {"q": "Ручьи"}),
                         ("reverse", {"lat": 60, "lon": 30})]:
        response = client.get("/api/v1/" + path, params={**params, "dummy": False})
        assert response.status_code == 503
