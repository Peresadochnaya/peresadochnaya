"""Long overnight walking results preserve the planner's actual street path."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.otp import OtpPlanner, decode_polyline
from app.schemas import RouteRequest


REQUEST = {
    "points": [{"lat": 59.9343, "lon": 30.3351}, {"lat": 60.0343, "lon": 30.5351}],
    "departure_at": "2026-11-13T02:00:00+03:00",
    "modes": ["walk"], "traffic_mode": "off",
}


def walking_response():
    # Synthetic protocol fixture, not a claim that this is a real 15 km path.
    fixture = Path(__file__).parents[1] / "app/fixtures/otp/plan-response.json"
    body = json.loads(fixture.read_text())
    leg = copy.deepcopy(body["data"]["planConnection"]["edges"][0]["node"]["legs"][0])
    leg.update(distance=15000, **{"from": {"name": "A", **REQUEST["points"][0], "stop": None},
                                "to": {"name": "B", **REQUEST["points"][1], "stop": None}})
    leg["start"]["scheduledTime"] = REQUEST["departure_at"]
    leg["end"]["scheduledTime"] = "2026-11-13T05:00:00+03:00"
    node = {"start": REQUEST["departure_at"], "end": leg["end"]["scheduledTime"],
            "duration": 10800, "numberOfTransfers": 0, "legs": [leg]}
    body["data"]["planConnection"]["edges"] = [{"node": node}]
    return body, leg


def plan(body, modes=None):
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OtpPlanner("http://otp", client=client).plan(
            RouteRequest.model_validate({**REQUEST, "modes": modes or ["walk"]}))
    return result, sent[0]["variables"]


@pytest.mark.parametrize("modes", [["walk"], ["bus", "walk"]])
def test_long_night_walk_preserves_duration_and_otp_geometry(modes):
    body, raw_leg = walking_response()
    result, variables = plan(body, modes)
    assert variables["modes"]["direct"] == ["WALK"]
    assert len(result.routes) == 1
    route = result.routes[0]
    assert route.duration_seconds == 10800  # Beyond access/egress's 30 minutes.
    assert {leg.mode for leg in route.legs} == {"walk"}
    assert route.transfers == 0 and route.distance_meters == 15000
    assert route.geometry.coordinates == decode_polyline(raw_leg["legGeometry"]["points"])
    assert result.sources.traffic_providers == []


def test_empty_otp_result_is_empty_and_preserves_routing_error():
    body = {"data": {"planConnection": {"edges": [], "routingErrors": [
        {"code": "NO_TRANSIT_CONNECTION", "description": "No connection found"}]}}}
    result, _ = plan(body)
    assert result.routes == []
    assert any("NO_TRANSIT_CONNECTION" in warning for warning in result.warnings)
