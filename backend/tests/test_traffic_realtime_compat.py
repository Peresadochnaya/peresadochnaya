"""GPS-delay feeds coexist with road correction without compounding delays."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.otp import OtpPlanner
from app.schemas import RouteRequest
from test_traffic_engine import Planner, TrafficEngine, request

FIXTURE = Path(__file__).resolve().parents[1] / 'app/fixtures/otp/plan-response.json'


def test_road_engine_requests_schedule_for_every_candidate_search():
    calls = []

    class RecordingPlanner(Planner):
        def plan(self, req, **kwargs):
            calls.append(kwargs)
            return super().plan(req, **kwargs)

    req = request().model_copy(update={'modes': ['bus', 'tram']})
    TrafficEngine(RecordingPlanner(), clock=lambda: 0).plan(req)
    assert len(calls) == 3
    assert all(call['exclude_realtime_updates'] is True for call in calls)


@pytest.mark.parametrize('scheduled_only', [False, True])
def test_schedule_opt_out_preserves_default_realtime_and_schedule_baseline(scheduled_only):
    body = json.loads(FIXTURE.read_text())
    node = body['data']['planConnection']['edges'][0]['node']
    for leg in node['legs']:
        for key in ('start', 'end'):
            scheduled = datetime.fromisoformat(leg[key]['scheduledTime'])
            leg[key]['estimated'] = {'time': (scheduled + timedelta(minutes=5)).isoformat(), 'delay': 300}
    payloads = []

    def handler(req):
        payloads.append(json.loads(req.content))
        return httpx.Response(200, json=body)

    planner = OtpPlanner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    req = RouteRequest(points=[{'lat': 59.9343, 'lon': 30.3351}, {'lat': 59.9447, 'lon': 30.2583}],
                       departure_at='2026-11-11T12:00:00+03:00')
    result = planner.plan(req, exclude_realtime_updates=scheduled_only)
    preference = payloads[0]['variables']['preferences']
    assert 'timetable' not in preference
    assert preference['transit'].get('timetable') == ({'excludeRealTimeUpdates': True} if scheduled_only else None)
    expected = node['legs'][0]['start']['scheduledTime' if scheduled_only else 'estimated']
    if not scheduled_only:
        expected = expected['time']
    assert result.routes[0].legs[0].start_at == datetime.fromisoformat(expected)
    if scheduled_only:
        assert result.routes[0].start_at == result.routes[0].legs[0].start_at
        assert result.routes[0].end_at == result.routes[0].legs[-1].end_at


def test_schedule_opt_out_rejects_missing_scheduled_time():
    from app.otp import _time
    with pytest.raises(ValueError, match='Missing leg time'):
        _time({'estimated': {'time': '2026-11-11T12:00:00+03:00'}}, scheduled_only=True)


def _assert_recorded_input_fields(value, type_name, contract):
    """Validate captured OTP input fields independently of adapter code."""
    fields = {field['name']: field['type'] for field in contract['types'][type_name]['inputFields']}
    assert value.keys() <= fields.keys(), f'Unsupported {type_name} fields: {value.keys() - fields.keys()}'
    for name, child in value.items():
        field_type = fields[name]
        while field_type.get('ofType'):
            field_type = field_type['ofType']
        if field_type['kind'] == 'INPUT_OBJECT' and field_type['name'] in contract['types']:
            _assert_recorded_input_fields(child, field_type['name'], contract)
        elif field_type['kind'] == 'ENUM' and field_type['name'] in contract['types']:
            allowed = {item['name'] for item in contract['types'][field_type['name']]['enumValues']}
            assert set(child if isinstance(child, list) else [child]) <= allowed
        elif field_type['kind'] == 'SCALAR' and field_type['name'] == 'Boolean':
            assert type(child) is bool


@pytest.mark.parametrize('modes', [['walk'], ['walk','bus'], ['bus']])
def test_traffic_request_matches_recorded_live_otp_input_contract(modes):
    contract = json.loads((Path(__file__).parent/'fixtures/otp-live-input-contract.json').read_text())
    payloads = []
    def handler(req):
        payloads.append(json.loads(req.content)['variables'])
        return httpx.Response(200,json={'data':{'planConnection':{'edges':[],'routingErrors':[]}}})
    planner = OtpPlanner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    planner.plan(request().model_copy(update={'modes':modes}),exclude_realtime_updates=True)
    variables = payloads[0]
    _assert_recorded_input_fields(variables['preferences'], 'PlanPreferencesInput', contract)
    _assert_recorded_input_fields(variables['modes'], 'PlanModesInput', contract)
    assert variables['preferences']['transit']['timetable']['excludeRealTimeUpdates'] is True
    # The old root placement passed mocks but real OTP rejects it.
    with pytest.raises(AssertionError,match='Unsupported PlanPreferencesInput'):
        _assert_recorded_input_fields({'timetable':{'excludeRealTimeUpdates':True}},
                                      'PlanPreferencesInput',contract)
