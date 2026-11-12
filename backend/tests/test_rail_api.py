import hashlib
import json
from pathlib import Path
import shutil

from test_gtfs_api import api, publish

BUNDLE = Path(__file__).resolve().parents[2] / 'data/processed/rail/feed.zip'


def install(root):
    digest = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
    path = root / 'rail/versions' / digest
    path.mkdir(parents=True)
    shutil.copyfile(BUNDLE, path / 'feed.zip')
    (root / 'rail/current.json').write_text(json.dumps({'sha256': digest}))
    return digest


def test_rail_stops_lines_and_trip_details_share_native_ids(tmp_path):
    publish(tmp_path); sha = install(tmp_path); c = api(tmp_path)
    result = c.get('/api/v1/stops', params={'bbox': '29,59,31,61', 'dummy': False, 'modes': 'rail'}).json()
    assert result['items'] and all(s['stop_id'].startswith('rail:') for s in result['items'])
    stop_id = 'rail:s9602497'
    detail = c.get(f'/api/v1/stops/{stop_id}', params={'dummy': False}).json()
    assert detail['gtfs_version'] == sha
    assert detail['routes'] and all(r['mode'] == 'rail' for r in detail['routes'])
    line = c.get('/api/v1/transit-routes/' + detail['routes'][0]['route_id'], params={'dummy': False}).json()
    trip = c.get('/api/v1/trips/' + line['trips'][0]['trip_id'], params={'dummy': False})
    assert trip.status_code == 200 and trip.json()['trip']['stop_times']
    assert c.get('/api/v1/stops/b', params={'dummy': False}).json()['stop']['name'] == 'КОНЕЦ'


def test_rail_departures_stop_at_published_end_date(tmp_path):
    publish(tmp_path); install(tmp_path); c = api(tmp_path)
    path = '/api/v1/stops/rail:s9602497/departures'
    result = c.get(path, params={'dummy': False, 'departure_at': '2026-11-18T08:00:00+03:00'})
    assert result.status_code == 200, result.text
    assert result.json()['items']
    assert all(item['line']['mode'] == 'rail' for item in result.json()['items'])
    expired = c.get(path, params={'dummy': False, 'departure_at': '2026-11-20T08:00:00+03:00'})
    assert expired.status_code == 422
    demo = c.get('/api/v1/stops', params={'bbox': '29,59,31,61', 'dummy': True, 'modes': 'rail'}).json()
    assert demo['items'] == []
