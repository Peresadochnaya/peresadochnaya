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
    assert preference.get('timetable') == ({'excludeRealTimeUpdates': True} if scheduled_only else None)
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
