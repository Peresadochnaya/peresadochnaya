"""2GIS documentation-shaped offline fixtures. No API keys or live calls."""
import copy
import json
import logging
import socket
from datetime import datetime, timezone

import httpx
import pytest

from app.dgis import BY_ID_URL, GEOCODER_URL, ROUTER_URL, SUGGEST_URL, VIEWPORT, DgisAddresses, DgisRouter, driving_body
from app.schemas import RoadRouteRequest
from app.yandex import ProviderError


REQUEST = RoadRouteRequest(points=[{"lat": 60, "lon": 30}, {"lat": 60.02, "lon": 30.03}])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("DGIS_API_KEY", "dgis-fixture-only-key")
    def forbidden(*args, **kwargs):
        raise AssertionError("2GIS tests must not spend live API quota")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def catalog(items):
    return {"meta": {"code": 200, "api_version": "3.0"}, "result": {"items": items, "total": len(items)}}


def geo_fixture():
    return catalog([{"id": "123456_suffix", "name": "Кронверкский проспект, 49",
                     "full_name": "Санкт-Петербург, Кронверкский проспект, 49",
                     "point": {"lon": 30.310011, "lat": 59.956363}}])


def route_fixture():
    return {"status": "OK", "type": "result", "result": [{
        "type": "carrouting", "total_distance": 300.75, "total_duration": 30.5,
        "maneuvers": [
            {"type": "begin", "outcoming_path": {"geometry": [
                {"selection": "LINESTRING(30 60 9200,30.02 60.01 9220)"}]}},
            {"type": "crossroad", "outcoming_path": {"geometry": [
                {"selection": "LINESTRING(30.02 60.01,30.03 60.02)"}]}},
            {"type": "end"},
        ]}]}


def adapter(handler, kind=DgisAddresses):
    return kind(httpx.Client(transport=httpx.MockTransport(handler)))


def test_suggest_plural_endpoint_opaque_uri_and_one_request():
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url).split("?")[0] == SUGGEST_URL
        assert dict(request.url.params) == {
            "key": "dgis-fixture-only-key", "q": "Кронверкский", "page_size": "2", "locale": "ru_RU",
            "suggest_type": "route_endpoint",
            "type": "building,street,station,adm_div.city,adm_div.settlement,adm_div.district", **VIEWPORT}
        return httpx.Response(200, json=geo_fixture())
    result = adapter(handler).suggest("Кронверкский", 2)
    assert result.mode == result.provider == "2gis" and len(calls) == 1
    assert result.items[0].uri == "dgis:123456_suffix"
    assert result.items[0].subtitle.startswith("Санкт-Петербург")


@pytest.mark.parametrize("kwargs,url,params", [
    ({"q": "Кронверкский, 49"}, GEOCODER_URL, {"q": "Кронверкский, 49", "page_size": "1", **VIEWPORT}),
    ({"uri": "dgis:123456_suffix"}, BY_ID_URL, {"id": "123456_suffix"}),
    ({"lat": 60, "lon": 30}, GEOCODER_URL, {"lat": "60", "lon": "30", "page_size": "1"}),
])
def test_geocode_query_uri_and_reverse_are_single_requests(kwargs, url, params):
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url).split("?")[0] == url
        assert dict(request.url.params) == {"locale": "ru_RU", "fields": "items.point,items.full_address_name",
                                            "key": "dgis-fixture-only-key", **params}
        return httpx.Response(200, json=geo_fixture())
    result = adapter(handler).geocode(**kwargs)
    assert len(calls) == 1 and result.mode == result.provider == "2gis"
    place = result.items[0]
    assert place.lat == 59.956363 and place.lon == 30.310011
    assert place.precision is None


@pytest.mark.parametrize("payload", [catalog([]), {"meta": {"code": 404}}])
def test_catalog_empty_result_is_not_an_error(payload):
    provider = adapter(lambda r: httpx.Response(200, json=payload))
    assert provider.geocode(q="Несуществующее").items == []
    assert provider.suggest("Несуществующее", 5).items == []


@pytest.mark.parametrize("kwargs", [{}, {"q": "q", "uri": "dgis:1"}, {"lat": 60},
    {"uri": "dummy:suggest:1"}, {"uri": "ymapsbm1://geo?data=x"}, {"uri": "dgis:https://localhost"},
    {"uri": "dgis:1&key=secret"}, {"lat": 95, "lon": 30}])
def test_invalid_queries_are_rejected_without_provider(kwargs):
    provider = adapter(lambda r: pytest.fail("Invalid input spent a request"))
    with pytest.raises(ProviderError) as exc:
        provider.geocode(**kwargs)
    assert exc.value.status == 422


@pytest.mark.parametrize("logical_code,status", [(403, 502), (429, 503), (408, 504), (504, 504)])
def test_http_200_catalog_errors_are_not_silently_treated_as_no_results(logical_code, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"meta": {"code": logical_code, "error": {"message": "private-key"}}})
    with pytest.raises(ProviderError) as exc:
        adapter(handler).geocode(q="Адрес")
    assert exc.value.status == status and len(calls) == 1
    assert "private-key" not in str(exc.value)


@pytest.mark.parametrize("payload", [[], {}, {"meta": {"code": "200"}},
    {"meta": {"code": 200}, "result": {"items": None}},
    catalog([{"id": "123", "name": None}])])
def test_malformed_catalog_is_sanitized(payload):
    with pytest.raises(ProviderError) as exc:
        adapter(lambda r: httpx.Response(200, json=payload)).suggest("Адрес", 2)
    assert exc.value.status == 502


@pytest.mark.parametrize("point", [{"lat": True, "lon": 30}, {"lat": "60", "lon": 30}, {"lat": 95, "lon": 30}])
def test_malformed_geocoder_coordinates_are_rejected(point):
    payload = geo_fixture()
    payload["result"]["items"][0]["point"] = point
    with pytest.raises(ProviderError) as exc:
        adapter(lambda r: httpx.Response(200, json=payload)).geocode(q="Адрес")
    assert exc.value.status == 502


@pytest.mark.parametrize("traffic,departure,fields", [
    ("on", None, {"route_mode": "fastest", "traffic_mode": "jam"}),
    ("on", "2099-11-11T14:00:00+03:00", {"route_mode": "fastest", "traffic_mode": "statistics",
        "utc": int(datetime(2099, 11, 11, 11, tzinfo=timezone.utc).timestamp())}),
    ("off", "2000-01-01T00:00:00Z", {"route_mode": "shortest"}),
])
def test_router_post_time_mode_and_documented_wkt(traffic, departure, fields):
    request = REQUEST.model_copy(update={"traffic_mode": traffic})
    if departure:
        request.departure_at = datetime.fromisoformat(departure)
    calls = []
    def handler(req):
        calls.append(req)
        assert req.method == "POST" and str(req.url).split("?")[0] == ROUTER_URL
        assert dict(req.url.params) == {"key": "dgis-fixture-only-key"}
        assert json.loads(req.content) == {
            "points": [{"type": "stop", "lat": 60, "lon": 30}, {"type": "stop", "lat": 60.02, "lon": 30.03}],
            "transport": "driving", "output": "detailed", "locale": "ru", "alternative": 0, **fields}
        return httpx.Response(200, json=route_fixture())
    result = adapter(handler, DgisRouter).estimate(request)
    assert result.mode == result.provider == "2gis" and len(calls) == 1
    assert result.route.distance_meters == 300.75 and result.route.duration_seconds == 30.5
    assert result.route.geometry.coordinates == [(30, 60), (30.02, 60.01), (30.03, 60.02)]
    assert any("different road trajectories" in warning for warning in result.warnings)
    assert result.traffic_type == ("disabled" if traffic == "off" else "forecast" if departure else "realtime")
    assert result.departure_at is None if traffic == "off" else result.departure_at == request.departure_at


@pytest.mark.parametrize("status", ["ROUTE_NOT_FOUND", "ROUTE_DOES_NOT_EXISTS", "POINT_EXCLUDED", "ATTRACT_FAIL"])
def test_no_road_route_is_explicit(status):
    result = adapter(lambda r: httpx.Response(200, json={"type": "error", "status": status}), DgisRouter).estimate(REQUEST)
    assert result.route is None and result.warnings


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(status="FAIL"), lambda d: d.update(result=[]),
    lambda d: d["result"][0].update(total_duration=-1),
    lambda d: d["result"][0].update(total_distance=True),
    lambda d: d["result"][0].update(total_duration=float("nan")),
    lambda d: d["result"][0].update(type="transit"),
    lambda d: d["result"][0].update(maneuvers=[]),
    lambda d: d["result"][0]["maneuvers"][0]["outcoming_path"].update(geometry=[]),
    lambda d: d["result"][0]["maneuvers"][0]["outcoming_path"]["geometry"][0].update(selection="POINT(30 60)"),
    lambda d: d["result"][0]["maneuvers"][0]["outcoming_path"]["geometry"][0].update(selection="LINESTRING(30 95,30 60)"),
    lambda d: d["result"][0]["maneuvers"][0]["outcoming_path"]["geometry"][0].update(selection="LINESTRING(30 60)"),
    lambda d: d["result"][0]["maneuvers"][1]["outcoming_path"]["geometry"][0].update(selection="LINESTRING(31 60,31 61)"),
])
def test_invalid_road_result_never_returns_partial_or_synthetic(mutation):
    data = copy.deepcopy(route_fixture())
    mutation(data)
    provider = adapter(lambda r: httpx.Response(200, content=json.dumps(data)), DgisRouter)
    with pytest.raises(ProviderError) as exc:
        provider.estimate(REQUEST)
    assert exc.value.status == 502 and exc.value.code == "dgis_router_provider_error"


@pytest.mark.parametrize("http_status,expected", [(401, 502), (403, 502), (429, 503), (500, 502), (504, 504), (302, 502)])
def test_no_redirect_retry_or_raw_error_leaks(http_status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(http_status, json={"error": "private-key"}, headers={"location": "https://example.com"})
    provider = adapter(handler, DgisRouter)
    with pytest.raises(ProviderError) as exc:
        provider.estimate(REQUEST)
    assert exc.value.status == expected and len(calls) == 1
    assert "private-key" not in str(exc.value)


def test_timeout_and_missing_key_do_not_retry(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive URL with key", request=request)
    provider = adapter(handler)
    with pytest.raises(ProviderError) as exc:
        provider.suggest("Адрес", 5)
    assert exc.value.status == 504 and len(calls) == 1
    monkeypatch.delenv("DGIS_API_KEY")
    with pytest.raises(ProviderError) as exc:
        provider.suggest("Адрес", 5)
    assert exc.value.status == 503 and len(calls) == 1


def test_past_forecast_is_rejected_without_request():
    request = REQUEST.model_copy(update={"departure_at": datetime(2000, 1, 1, tzinfo=timezone.utc)})
    with pytest.raises(ProviderError) as exc:
        adapter(lambda r: pytest.fail("Past forecast spent quota"), DgisRouter).estimate(request)
    assert exc.value.status == 422


def test_httpx_logs_hide_2gis_query_key(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        adapter(lambda r: httpx.Response(200, json=geo_fixture())).geocode(q="Адрес")
    assert "dgis-fixture-only-key" not in caplog.text
    assert "[REDACTED]" in caplog.text
