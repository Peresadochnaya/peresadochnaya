"""Response contract checks: provenance, segment lengths, and metro estimates."""

import copy
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.schemas import Leg, MetroWaitEstimate, RoutesResponse

REQUEST = {"points": [{"lat": 60.002, "lon": 30.296}, {"lat": 60.036, "lon": 30.445}],
           "departure_at": "2026-11-10T15:00:00+03:00", "providers": ["yandex", "2gis", "otp"]}
ESTIMATE = {"line": "2", "day_type": "weekday", "period_start": "08:00", "period_end": "10:00",
            "min_headway_seconds": 120, "max_headway_seconds": 240,
            "representative_headway_seconds": 180, "estimated_wait_seconds": 90,
            "model": "half_representative_headway", "source_url": "https://metro.spb.ru/intervalpoezd01092026.html",
            "source_valid_from": "2026-09-01"}


@pytest.fixture
def response(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        result = client.post("/api/v1/routes", json=REQUEST)
    assert result.status_code == 200
    return result.json()


def test_dummy_declares_only_actual_sources_and_segment_distances(response):
    assert response["mode"] == "dummy"
    assert response["sources"] == {"candidate_providers": [], "traffic_providers": [], "realtime": "dummy"}
    route = response["routes"][0]
    assert route["provenance"] == {"candidate_providers": [], "traffic_providers": []}
    assert sum(leg["distance_meters"] for leg in route["legs"]) == pytest.approx(route["distance_meters"], abs=1e-6)
    assert all(leg["distance_meters"] > 0 and leg["metro_wait_estimate"] is None for leg in route["legs"])
    RoutesResponse.model_validate(response)


def test_zero_length_segments_for_identical_points(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        result = client.post("/api/v1/routes", json={**REQUEST, "points": [REQUEST["points"][0]] * 2}).json()
    assert result["routes"][0]["distance_meters"] == 0
    assert all(leg["distance_meters"] == 0 for leg in result["routes"][0]["legs"])


def test_real_mode_is_independent_from_traffic_and_realtime(response):
    data = copy.deepcopy(response)
    data.update(mode="real", sources={"candidate_providers": ["otp"], "traffic_providers": [], "realtime": "unavailable"})
    data["routes"][0]["provenance"]["candidate_providers"] = ["otp"]
    assert RoutesResponse.model_validate(data).mode == "real"
    data["mode"] = "planner"
    with pytest.raises(ValidationError):
        RoutesResponse.model_validate(data)


def test_duplicate_provider_provenance_is_rejected(response):
    response["routes"][0]["provenance"]["candidate_providers"] = ["otp", "otp"]
    with pytest.raises(ValidationError, match="duplicates"):
        RoutesResponse.model_validate(response)


def test_metro_estimate_roundtrips_and_is_restricted_to_waiting(response):
    estimate = MetroWaitEstimate.model_validate(ESTIMATE)
    assert estimate.model_dump(mode="json")["source_valid_from"] == "2026-09-01"
    leg = {**response["routes"][0]["legs"][0], "mode": "wait", "duration_source": "estimated",
           "distance_meters": 0, "duration_seconds": 90, "original_duration_seconds": 90,
           "end_at": "2026-11-10T15:01:30+03:00", "metro_wait_estimate": ESTIMATE}
    assert Leg.model_validate(leg).metro_wait_estimate.estimated_wait_seconds == 90
    for change in ({"mode": "bus"}, {"distance_meters": 10}, {"duration_source": "schedule"}):
        with pytest.raises(ValidationError):
            Leg.model_validate({**leg, **change})


@pytest.mark.parametrize("change", [{"min_headway_seconds": 0}, {"max_headway_seconds": 100},
    {"estimated_wait_seconds": 180}, {"representative_headway_seconds": float("inf")},
    {"model": "exact_departure"}, {"source_valid_from": "tomorrow"}])
def test_incoherent_metro_estimate_is_rejected(change):
    with pytest.raises(ValidationError):
        MetroWaitEstimate.model_validate({**ESTIMATE, **change})


@pytest.mark.parametrize("source", ["estimated", "2gis"])
def test_new_duration_sources_are_accepted(response, source):
    assert Leg.model_validate({**response["routes"][0]["legs"][0], "duration_source": source}).duration_source == source


def test_root_openapi_matches_served_contract(tmp_path):
    root = Path(__file__).resolve().parents[2]
    with TestClient(create_app(tmp_path)) as client:
        actual = client.get("/openapi.json").json()
    assert yaml.safe_load((root / "openapi.yaml").read_text()) == actual
    schemas = actual["components"]["schemas"]
    assert {"RouteProvenance", "RoutesResponseSources", "MetroWaitEstimate"} <= schemas.keys()
    assert "provenance" in schemas["Route"]["required"]
    assert "sources" in schemas["RoutesResponse"]["required"]
    assert "distance_meters" in schemas["Leg"]["required"]
    assert schemas["RoutesResponse"]["properties"]["mode"]["enum"] == ["dummy", "real"]
    assert {"502", "503", "504"} <= actual["paths"]["/api/v1/routes"]["post"]["responses"].keys()
