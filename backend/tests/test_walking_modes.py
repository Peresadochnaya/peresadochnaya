"""Offline coverage of explicit walking requests and legacy transit defaults."""
import json
import socket
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from app.demo_routes import compose
from app.otp import OtpPlanner
from app.schemas import RouteRequest
from app.traffic_engine import TrafficEngine

TRANSIT = ['bus', 'tram', 'trolleybus', 'metro', 'rail']


def request(**options):
    return RouteRequest(points=[{'lat':59.93,'lon':30.33}, {'lat':59.95,'lon':30.30}],
                        departure_at=datetime(2026,11,13,tzinfo=timezone.utc), **options)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('No network allowed'))


def planner_and_calls():
    calls = []
    def handle(req):
        calls.append(json.loads(req.content)['variables'])
        return httpx.Response(200,json={'data':{'planConnection':{'edges':[], 'routingErrors':[]}}})
    return OtpPlanner('http://otp',client=httpx.Client(transport=httpx.MockTransport(handle))), calls


@pytest.mark.parametrize('options', [{}, {'modes':None}])
def test_legacy_default_keeps_all_transit(options):
    planner, calls = planner_and_calls()
    planner.plan(request(**options))
    assert len(calls[0]['modes']['transit']['transit']) == 5
    assert 'directOnly' not in calls[0]['modes']


@pytest.mark.parametrize('options', [
    {'modes':['walk']}, {'exclude_modes':TRANSIT},
    {'modes':['walk','bus'],'exclude_modes':['bus']},
])
def test_no_transit_uses_direct_only_walk(options):
    planner, calls = planner_and_calls()
    planner.plan(request(**options))
    assert calls[0]['modes'] == {'direct':['WALK'], 'directOnly':True}


def test_mixed_modes_request_direct_walk_and_selected_transit():
    planner, calls = planner_and_calls()
    planner.plan(request(modes=['walk','bus','metro'],exclude_modes=['metro']))
    modes = calls[0]['modes']
    assert modes['direct'] == ['WALK']
    assert modes['transit']['transit'] == [{'mode':'BUS'}]
    assert 'directOnly' not in modes


@pytest.mark.parametrize('options', [
    {'modes':[]}, {'modes':['walk','walk']}, {'exclude_modes':['walk']},
])
def test_invalid_selection_rejected(options):
    with pytest.raises(ValidationError):
        request(**options)


def test_walk_and_all_transit_are_accepted():
    assert len(request(modes=['walk',*TRANSIT]).modes) == 6


@pytest.mark.parametrize('options', [{'modes':['walk']}, {'exclude_modes':TRANSIT}])
def test_traffic_on_walking_never_collects_car_observations(options):
    planner, calls = planner_and_calls()
    def forbidden(*args, **kwargs):
        pytest.fail('Walking must not load graph or collect paid observations')
    response = TrafficEngine(planner,collector=forbidden,graph_loader=forbidden).plan(
        request(traffic_mode='on',**options))
    assert len(calls) == 1
    assert response.routes == []
    assert response.sources.traffic_providers == []
    assert response.debug.pipeline_status == "complete"
    assert response.debug.candidate_count == 0
    assert len(response.debug.provider_calls) == 1
    assert not any('traffic' in warning.lower() for warning in response.warnings)


def test_mixed_traffic_diversity_includes_walk_without_redundant_fallback():
    planner, calls = planner_and_calls()
    TrafficEngine(planner).plan(request(modes=['walk','bus','metro'],traffic_mode='on'))
    assert len(calls) == 4
    assert calls[0]['modes']['direct'] == ['WALK']
    assert calls[1]['modes'] == {'direct':['WALK'], 'directOnly':True}
    assert all(call['modes']['direct'] == ['WALK'] for call in calls[2:])
    assert all(len(call['modes']['transit']['transit']) == 1 for call in calls[2:])


def test_dummy_walk_has_only_walk_legs_without_network():
    result = compose(request(modes=['walk'],traffic_mode='on'))
    assert [leg.mode for leg in result.legs] == ['walk']
    assert result.legs[0].duration_source == 'dummy'
    assert result.provenance.traffic_providers == []


def test_empty_walk_result_is_explicit_without_fabricated_fallback():
    planner, calls = planner_and_calls()
    response = planner.plan(request(modes=['walk']))
    assert response.routes == []
    assert any('search limits' in warning for warning in response.warnings)
    assert len(calls) == 1


def test_walking_traffic_diagnostics_are_not_applicable():
    planner, _ = planner_and_calls()
    response = planner.plan(request(modes=['walk']))
    response.routes = [compose(request(modes=['walk']))]
    class FixturePlanner:
        def plan(self, req, **kwargs):
            return response
    result = TrafficEngine(FixturePlanner()).plan(request(modes=['walk'],traffic_mode='on'))
    assert result.debug.returned_count == 1
    assert result.routes[0].debug.evaluation_group == 'not_applicable'
    assert result.routes[0].debug.coverage.eligible_distance_meters == 0
    assert all(leg.debug.traffic_status == 'not_applicable' for leg in result.routes[0].legs)


def test_transit_without_explicit_walk_requests_direct_walk_but_does_not_force_extra_result():
    from pathlib import Path
    body = json.loads((Path(__file__).parents[1] / 'app/fixtures/otp/plan-response.json').read_text())
    calls = []
    def handler(req):
        calls.append(json.loads(req.content)['variables'])
        return httpx.Response(200, json=body)
    result = OtpPlanner('http://otp', client=httpx.Client(transport=httpx.MockTransport(handler))).plan(
        request(modes=['bus'], max_results=1))
    assert len(calls) == 1
    assert calls[0]['modes']['direct'] == ['WALK']
    assert len(result.routes) == 1
    assert any(leg.mode == 'bus' for leg in result.routes[0].legs)


def test_empty_transit_search_falls_back_to_real_walking_route():
    from test_walking_night import walking_response
    body, _ = walking_response()
    calls = []
    def handler(req):
        variables = json.loads(req.content)['variables']
        calls.append(variables)
        return httpx.Response(200, json=(body if variables['modes'].get('directOnly') else
                              {'data': {'planConnection': {'edges': [], 'routingErrors': []}}}))
    result = OtpPlanner('http://otp', client=httpx.Client(transport=httpx.MockTransport(handler))).plan(
        request(modes=['bus'], max_results=1))
    assert len(calls) == 2
    assert calls[1]['modes'] == {'direct':['WALK'], 'directOnly':True}
    assert len(result.routes) == 1
    assert all(leg.mode == 'walk' for leg in result.routes[0].legs)
    assert not any('found no route' in warning for warning in result.warnings)


def test_walking_fallback_timeout_is_diagnostic_and_never_fabricates_route():
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 2:
            raise httpx.ReadTimeout('walking timeout')
        return httpx.Response(200, json={'data': {'planConnection': {'edges': [], 'routingErrors': []}}})
    result = OtpPlanner('http://otp', client=httpx.Client(transport=httpx.MockTransport(handler))).plan(
        request(modes=['bus']), timeout_seconds=2)
    assert result.routes == []
    assert any('Walking fallback failed: planner_timeout' in warning for warning in result.warnings)
    assert len(calls) == 2
    assert calls[0].extensions['timeout']['read'] <= 1.6
    assert calls[1].extensions['timeout']['read'] <= 2


@pytest.mark.parametrize('limit', [1, 10])
def test_walking_competes_with_transit_under_shared_limit(limit):
    import copy
    from pathlib import Path
    from test_walking_night import walking_response
    body = json.loads((Path(__file__).parents[1] / 'app/fixtures/otp/plan-response.json').read_text())
    original = body['data']['planConnection']['edges'][0]
    edges = []
    for index in range(11):
        edge = copy.deepcopy(original)
        edge['node']['duration'] += index
        edges.append(edge)
    walk_body, _ = walking_response()
    walk = walk_body['data']['planConnection']['edges'][0]
    walk['node']['duration'] = 1
    body['data']['planConnection']['edges'] = edges + [walk]
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)
    result = OtpPlanner('http://otp', client=httpx.Client(transport=httpx.MockTransport(handler))).plan(
        request(modes=['bus'], max_results=limit))
    assert len(result.routes) == limit
    assert all(leg.mode == 'walk' for leg in result.routes[0].legs)
    assert len(calls) == 1
