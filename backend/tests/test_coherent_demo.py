"""Cross-endpoint behavior and identities of the shared frontend demo."""

from datetime import datetime, timedelta, timezone
import socket

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.vehicles_api import demo_positions


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("Demo must never access the network"))
    with TestClient(create_app(tmp_path)) as api:
        yield api


@pytest.mark.parametrize("provider", ["yandex", "2gis"])
def test_true_geocoding_routing_departures_and_vehicles_share_one_network(client, provider):
    lines = client.get('/api/v1/transit-routes', params={'dummy': True}).json()
    assert lines['mode'] == 'dummy' and lines['items']
    line = lines['items'][0]
    detail = client.get('/api/v1/transit-routes/' + line['route_id'], params={'dummy': True}).json()
    trip_id = detail['trips'][0]['trip_id']
    trip = client.get('/api/v1/trips/' + trip_id, params={'dummy': True}).json()['trip']
    first, last = trip['stop_times'][2]['stop'], trip['stop_times'][10]['stop']
    suggestions = client.get('/api/v1/suggest', params={'q': first['name'], 'provider': provider, 'dummy': True}).json()
    assert suggestions['mode'] == 'dummy'
    selected = next(item for item in suggestions['items'] if item['uri'].endswith(':stop:' + first['stop_id']))
    point = client.get('/api/v1/geocode', params={'uri': selected['uri'], 'provider': provider, 'dummy': True}).json()['items'][0]
    assert (point['lat'], point['lon'], point['label']) == (first['lat'], first['lon'], first['name'])
    reverse = client.get('/api/v1/reverse', params={'lat': point['lat'], 'lon': point['lon'], 'provider': provider, 'dummy': True}).json()
    assert reverse['items'][0] == point
    body = {'points': [{k:first[k] for k in ('lat','lon')}, {k:last[k] for k in ('lat','lon')}],
            'departure_at': '2026-11-11T12:00:00+03:00'}
    response = client.post('/api/v1/routes?dummy=true', json=body)
    assert response.status_code == 200
    route = response.json()['routes'][0]
    bus = next(leg for leg in route['legs'] if leg['mode'] == 'bus')
    actual_trip = client.get('/api/v1/trips/' + bus['line']['trip_id'], params={'dummy': True}).json()['trip']
    calls = {row['stop']['stop_id']:row for row in actual_trip['stop_times']}
    assert bus['line']['route_id'] == line['route_id'] == actual_trip['route_id']
    for place in (bus['from'],bus['to']):
        stop = calls[place['stop_id']]['stop']
        assert (place['name'], place['lat'], place['lon']) == (stop['name'], stop['lat'], stop['lon'])
    wait = next(leg for leg in route['legs'] if leg['mode'] == 'wait')
    departures = client.get('/api/v1/stops/' + bus['from']['stop_id'] + '/departures',
        params={'departure_at':wait['start_at'], 'dummy':True, 'limit':100}).json()
    assert any(row['trip_id'] == bus['line']['trip_id'] and row['scheduled_departure_at'] == bus['start_at']
               for row in departures['items'])
    for left, right in zip(route['legs'],route['legs'][1:]):
        assert left['end_at'] == right['start_at']
        assert left['geometry']['coordinates'][-1] == right['geometry']['coordinates'][0]
    assert route['duration_seconds'] == sum(x['duration_seconds'] for x in route['legs'])
    assert route['distance_meters'] == pytest.approx(sum(x['distance_meters'] for x in route['legs']))
    vehicles = client.get('/api/v1/vehicles', params={'bbox':'29,59,31,61','dummy':True}).json()
    assert vehicles['mode'] == 'dummy' and vehicles['items'] and not vehicles['stale']
    assert all(v['route_id'] == line['route_id'] for v in vehicles['items'])
    assert {v['trip_id'] for v in vehicles['items']} <= {t['trip_id'] for t in detail['trips']}
    road = client.post('/api/v1/road-route', params={'provider':provider,'dummy':True}, json={'points':body['points']}).json()
    assert road['mode'] == 'dummy' and road['provider'] == provider
    assert road['route']['geometry']['coordinates'][0] == [first['lon'],first['lat']]
    assert road['route']['geometry']['coordinates'][-1] == [last['lon'],last['lat']]


def test_all_public_switches_default_true_and_describe_real_opt_in(client):
    schema = client.get('/openapi.json').json()
    count = 0
    for path in schema['paths'].values():
        for operation in path.values():
            for parameter in operation.get('parameters',[]):
                if parameter['name'] == 'dummy':
                    assert parameter['schema']['default'] is True
                    count += 1
    assert count == 12


@pytest.mark.parametrize('patch', [{'modes':['metro']}, {'exclude_modes':['bus']}])
def test_demo_respects_excluded_bus(client, patch):
    body = {'points':[{'lat':60.01,'lon':30.49},{'lat':60.04,'lon':30.41}],
            'departure_at':'2026-11-11T12:00:00+03:00', **patch}
    result = client.post('/api/v1/routes?dummy=true',json=body).json()
    assert result['mode'] == 'dummy'
    assert {leg['mode'] for leg in result['routes'][0]['legs']} == {'walk'}


def test_fractional_departure_is_rounded_up_to_consistent_whole_seconds(client):
    body = {'points':[{'lat':60.01,'lon':30.49},{'lat':60.04,'lon':30.41}],
            'departure_at':'2026-11-11T12:00:00.123+03:00'}
    result = client.post('/api/v1/routes?dummy=true',json=body).json()['routes'][0]
    assert result['start_at'] == '2026-11-11T12:00:01+03:00'
    for leg in result['legs']:
        assert (datetime.fromisoformat(leg['end_at'])-datetime.fromisoformat(leg['start_at'])).total_seconds() == leg['duration_seconds']


def test_demo_vehicles_move_on_the_same_departure_grid(tmp_path):
    now = datetime(2026,11,11,9,tzinfo=timezone.utc)
    first = demo_positions(tmp_path,now)
    second = demo_positions(tmp_path,now+timedelta(minutes=3))
    assert first and second
    assert {v.trip_id for v in first} == {v.trip_id for v in second}
    assert {(v.trip_id,v.lat,v.lon) for v in first} != {(v.trip_id,v.lat,v.lon) for v in second}
    assert all(v.timestamp == now for v in first)
