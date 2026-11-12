import csv
import hashlib
import io
import json
import shutil
import socket
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gtfs_api import create_gtfs_router
from app.gtfs_store import DEMO_GTFS_PATH, GtfsStore, service_instant


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('GTFS API is local-only')
    monkeypatch.setattr(socket.socket,'connect',forbidden)


def tables():
    return {
        'agency.txt':[{'agency_id':'agency','agency_name':'Test','agency_url':'https://example.test','agency_timezone':'Europe/Moscow'}],
        'stops.txt':[
            {'stop_id':'a','stop_name':'Начало','stop_lat':'59.9','stop_lon':'30.1','location_type':'0','stop_code':'100'},
            {'stop_id':'b','stop_name':'КОНЕЦ','stop_lat':'60.0','stop_lon':'30.2','location_type':'0','stop_code':'200'},
            {'stop_id':'c','stop_name':'Вне области','stop_lat':'65.0','stop_lon':'40.0','location_type':'0','stop_code':'300'},
        ],
        'routes.txt':[
            {'route_id':'r1','agency_id':'agency','route_short_name':'193','route_long_name':'Начало — конец','route_type':'3'},
            {'route_id':'r2','agency_id':'agency','route_short_name':'T','route_long_name':'Трамвай','route_type':'0'},
        ],
        'trips.txt':[
            {'trip_id':'t1','route_id':'r1','service_id':'service','direction_id':'0','shape_id':'shape','trip_headsign':'Конец'},
            {'trip_id':'t2','route_id':'r2','service_id':'service','direction_id':'1','shape_id':'','trip_headsign':'Начало'},
        ],
        'stop_times.txt':[
            {'trip_id':'t1','stop_id':'a','arrival_time':'24:50:00','departure_time':'24:50:00','stop_sequence':'0'},
            {'trip_id':'t1','stop_id':'b','arrival_time':'25:10:00','departure_time':'25:10:00','stop_sequence':'1'},
            {'trip_id':'t2','stop_id':'a','arrival_time':'01:00:00','departure_time':'01:00:00','stop_sequence':'0'},
            {'trip_id':'t2','stop_id':'b','arrival_time':'01:15:00','departure_time':'01:15:00','stop_sequence':'1'},
        ],
        'shapes.txt':[
            {'shape_id':'shape','shape_pt_lat':'60.0','shape_pt_lon':'30.2','shape_pt_sequence':'1'},
            {'shape_id':'shape','shape_pt_lat':'59.9','shape_pt_lon':'30.1','shape_pt_sequence':'0'},
        ],
        'calendar.txt':[dict(service_id='service',**{day:'1' for day in ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')},start_date='20260101',end_date='20261231')],
        'calendar_dates.txt':[],
    }


def publish(root, data=None):
    data=tables() if data is None else data
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,rows in data.items():
            fields=list(dict.fromkeys(k for row in rows for k in row)) or ['service_id','date','exception_type']
            text=io.StringIO();writer=csv.DictWriter(text,fieldnames=fields);writer.writeheader();writer.writerows(rows)
            z.writestr(name,text.getvalue())
    payload=buffer.getvalue();sha=hashlib.sha256(payload).hexdigest()
    version=root/'gtfs'/'versions'/sha;version.mkdir(parents=True,exist_ok=True);(version/'feed.zip').write_bytes(payload)
    (root/'gtfs'/'current.json').write_text(json.dumps({'sha256':sha}))
    return sha,version


def api(root):
    app=FastAPI();app.include_router(create_gtfs_router(root));return TestClient(app)


def test_collections_filter_before_pagination_and_details_share_ids(tmp_path):
    sha,_=publish(tmp_path);c=api(tmp_path)
    r=c.get('/api/v1/stops',params={'dummy':False,'bbox':'30,59,31,61','q':'конец','modes':'tram','limit':1}).json()
    assert r['mode']=='gtfs' and r['gtfs_version']==sha
    assert [s['stop_id'] for s in r['items']]==['b'] and r['total']==1 and not r['has_more']
    paged=c.get('/api/v1/stops',params={'dummy':False,'bbox':'30,59,31,61','limit':1,'offset':1}).json()
    assert paged['items'][0]['stop_id']=='b' and paged['total']==2
    detail=c.get('/api/v1/stops/b',params={'dummy':False}).json()
    assert detail['stop']['name']=='КОНЕЦ'
    assert [r['route_id'] for r in detail['routes']]==['r1','r2']
    lines=c.get('/api/v1/transit-routes',params={'dummy':False,'modes':'bus','q':'193'}).json()
    assert lines['total']==1 and lines['items'][0]['route_id']=='r1'
    line=c.get('/api/v1/transit-routes/r1',params={'dummy':False}).json()
    assert line['trips'][0]['trip_id']=='t1' and line['directions']==[{'direction_id':0,'headsign':'Конец'}]


def test_trip_preserves_gtfs_over_24_times_and_shapes_order(tmp_path):
    publish(tmp_path);c=api(tmp_path)
    r=c.get('/api/v1/trips/t1',params={'dummy':False}).json()
    assert r['trip']['stop_times'][1]['departure_time']=='25:10:00'
    assert r['trip']['geometry']['coordinates']==[[30.1,59.9],[30.2,60.0]]
    other=c.get('/api/v1/trips/t2',params={'dummy':False}).json()
    assert other['trip']['geometry'] is None and any('shape_unavailable' in w for w in other['warnings'])


def test_departures_include_previous_service_day_and_sorted_window(tmp_path):
    publish(tmp_path);c=api(tmp_path)
    p={'dummy':False,'departure_at':'2026-11-11T00:50:00+03:00','lookahead_minutes':60,'limit':1}
    r=c.get('/api/v1/stops/b/departures',params=p)
    assert r.status_code==200,r.text
    d=r.json();assert d['has_more'] and len(d['items'])==1
    first=d['items'][0];assert first['trip_id']=='t1' and first['service_date']=='2026-11-10'
    assert first['scheduled_departure_at']=='2026-11-11T01:10:00+03:00'
    p['limit']=10;r=c.get('/api/v1/stops/b/departures',params=p).json()
    assert [i['trip_id'] for i in r['items']]==['t1','t2']


def test_calendar_exception_overrides_add_and_remove_and_pickup(tmp_path):
    t=tables();t['calendar_dates.txt']=[{'service_id':'service','date':'20261110','exception_type':'2'}]
    t['stop_times.txt'][3]['pickup_type']='1'
    publish(tmp_path,t);c=api(tmp_path)
    r=c.get('/api/v1/stops/b/departures',params={'dummy':False,'departure_at':'2026-11-11T00:50:00+03:00'}).json()
    assert r['items']==[]
    # A date exception may activate an otherwise malformed base calendar.
    t['calendar.txt'][0]['end_date']='20190101';t['calendar_dates.txt']=[{'service_id':'service','date':'20261110','exception_type':'1'}]
    publish(tmp_path,t)
    r=c.get('/api/v1/stops/b/departures',params={'dummy':False,'departure_at':'2026-11-11T00:50:00+03:00'})
    assert r.status_code==200 and r.json()['items'][0]['trip_id']=='t1'


def test_invalid_calendar_is_not_empty_success_and_valid_empty_window_is(tmp_path):
    t=tables();t['calendar.txt'][0]['end_date']='20190101';publish(tmp_path,t);c=api(tmp_path)
    p={'dummy':False,'departure_at':'2026-11-11T12:00:00+03:00'}
    r=c.get('/api/v1/stops/b/departures',params=p)
    assert r.status_code==422 and r.json()['error']['code']=='invalid_service_date'
    publish(tmp_path);r=c.get('/api/v1/stops/b/departures',params=p)
    assert r.status_code==200 and r.json()['items']==[]


def test_frequency_window_is_not_fabricated_exact_departure(tmp_path):
    t=tables();t['frequencies.txt']=[{'trip_id':'t2','start_time':'01:00:00','end_time':'02:00:00','headway_secs':'600','exact_times':'0'}]
    publish(tmp_path,t);c=api(tmp_path)
    r=c.get('/api/v1/stops/b/departures',params={'dummy':False,'departure_at':'2026-11-11T01:20:00+03:00','lookahead_minutes':30}).json()
    assert len(r['items'])==1
    d=r['items'][0];assert d['schedule_type']=='frequency' and d['scheduled_departure_at'] is None and d['departure_time'] is None
    assert d['headway_seconds']==600 and d['frequency_start_at']=='2026-11-11T01:15:00+03:00'
    assert d['frequency_end_at']=='2026-11-11T02:15:00+03:00'
    assert any('not a precise' in w for w in r['warnings'])


def test_exact_frequency_grid_offsets_to_requested_stop(tmp_path):
    t=tables();t['frequencies.txt']=[{'trip_id':'t2','start_time':'01:00:00','end_time':'02:00:00','headway_secs':'600','exact_times':'1'}]
    publish(tmp_path,t);c=api(tmp_path)
    r=c.get('/api/v1/stops/b/departures',params={'dummy':False,'departure_at':'2026-11-11T01:20:00+03:00','lookahead_minutes':30}).json()
    assert [d['departure_time'] for d in r['items']]==['01:25:00','01:35:00','01:45:00']
    assert all(d['schedule_type']=='timetable' for d in r['items'])


def test_dst_service_day_uses_noon_minus_twelve_elapsed_hours(tmp_path):
    t=tables();t['agency.txt'][0]['agency_timezone']='Europe/Berlin'
    t['stop_times.txt'][1]['departure_time']='02:30:00'
    publish(tmp_path,t);c=api(tmp_path)
    r=c.get('/api/v1/stops/b/departures',params={'dummy':False,'departure_at':'2026-10-25T02:20:00+01:00','lookahead_minutes':20}).json()
    assert len(r['items'])==1 and r['items'][0]['scheduled_departure_at']=='2026-10-25T02:30:00+01:00'
    at=service_instant(date(2026,3,29),0,ZoneInfo('Europe/Berlin'))
    assert at.astimezone(timezone.utc)==datetime(2026,3,28,22,tzinfo=timezone.utc)


def test_missing_corrupt_and_traversal_snapshot_return_safe_errors(tmp_path):
    c=api(tmp_path)
    r=c.get('/api/v1/transit-routes',params={'dummy':False});assert r.status_code==503
    (tmp_path/'gtfs').mkdir();(tmp_path/'gtfs'/'current.json').write_text('{"sha256":"../../secret"}')
    r=c.get('/api/v1/transit-routes',params={'dummy':False});assert r.status_code==503 and str(tmp_path) not in r.text
    sha,folder=publish(tmp_path);(folder/'feed.zip').write_bytes(b'not zip')
    assert c.get('/api/v1/transit-routes',params={'dummy':False}).status_code==503


@pytest.mark.parametrize('path', ['/api/v1/stops/missing','/api/v1/transit-routes/missing','/api/v1/trips/missing'])
def test_unknown_ids_are_404(tmp_path,path):
    publish(tmp_path);assert api(tmp_path).get(path,params={'dummy':False}).status_code==404


@pytest.mark.parametrize('params',[
    {'bbox':'31,60,30,59'}, {'bbox':'-181,0,0,1'}, {'bbox':'30,59,31,91'}, {'bbox':'nan,0,1,1'},
    {'bbox':'30,59,31,61','modes':'hovercraft'}, {'bbox':'30,59,31,61','limit':0},
    {'bbox':'30,59,31,61','offset':-1}, {'bbox':'30,59,31,61','q':'   '},
])
def test_invalid_collection_inputs_are_422(tmp_path,params):
    publish(tmp_path);assert api(tmp_path).get('/api/v1/stops',params={**params,'dummy':False}).status_code==422


def test_demo_is_self_contained_and_connected_with_real_spb_stops(tmp_path):
    c=api(tmp_path)
    stops=c.get('/api/v1/stops',params={'bbox':'29,59,32,61'}).json()
    assert stops['mode']=='dummy' and stops['warnings'] and stops['total']==38
    lines=c.get('/api/v1/transit-routes').json();assert lines['items'][0]['short_name']=='193'
    line=c.get('/api/v1/transit-routes/3812').json()
    trip=c.get('/api/v1/trips/'+line['trips'][0]['trip_id']).json()
    assert len(trip['trip']['geometry']['coordinates'])>2
    stop=trip['trip']['stop_times'][0]['stop']
    assert stop['stop_id'] in {s['stop_id'] for s in stops['items']}
    departure=c.get('/api/v1/stops/'+stop['stop_id']+'/departures',params={'departure_at':'2026-11-11T05:00:00+03:00','lookahead_minutes':120}).json()
    assert departure['items'] and departure['mode']=='dummy'


def test_open_snapshot_survives_publication_and_removal_of_old_directory(tmp_path):
    sha,folder=publish(tmp_path);store=GtfsStore(tmp_path)
    with store.open() as old:
        t=tables();t['routes.txt'][0]['route_short_name']='NEW';newsha,_=publish(tmp_path,t);shutil.rmtree(folder)
        assert old.version==sha and old.routes['r1'].short_name=='193'
        assert old.trip('t1')[0].stop_times
        with store.open() as new:assert new.version==newsha and new.routes['r1'].short_name=='NEW'
    assert len(store._cache)==1


def test_exact_frequency_generates_only_bounded_prefix(tmp_path,monkeypatch):
    import app.gtfs_store as module
    t=tables();t['frequencies.txt']=[{'trip_id':'t2','start_time':'00:00:00','end_time':'24:00:00','headway_secs':'1','exact_times':'1'}]
    publish(tmp_path,t)
    real=module.GtfsDeparture;constructed=[]
    def count(*args,**kwargs):
        constructed.append(1);return real(*args,**kwargs)
    monkeypatch.setattr(module,'GtfsDeparture',count)
    response=api(tmp_path).get('/api/v1/stops/b/departures',params={'dummy':False,'departure_at':'2026-11-11T01:20:00+03:00','lookahead_minutes':1440,'limit':2})
    assert response.status_code==200
    assert response.json()['has_more'] and len(response.json()['items'])==2
    assert len(constructed)<20


def graph_calendar_policy(root, **overrides):
    meta = {'gtfs_calendar_fixed': True, 'activated_at': '2026-11-11T12:00:00Z',
            'gtfs_service_end': '2026-12-31', 'gtfs_source_sha256': 'a' * 64}
    meta.update(overrides)
    path = root / 'otp/current.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(meta))


def test_departures_use_explicit_loaded_graph_policy_for_inverted_calendar(tmp_path):
    t = tables(); t['calendar.txt'][0]['end_date'] = '20190101'
    _, version = publish(tmp_path, t)
    source = (version / 'feed.zip').read_bytes()
    graph_calendar_policy(tmp_path)
    response = api(tmp_path).get('/api/v1/stops/b/departures', params={
        'dummy': False, 'departure_at': '2026-11-11T00:50:00+03:00'})
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['trip_id'] == 't1'
    assert any('calendar_repaired:' in w for w in response.json()['warnings'])
    assert (version / 'feed.zip').read_bytes() == source


@pytest.mark.parametrize('overrides', [
    {'activated_at': None}, {'gtfs_calendar_fixed': False},
    {'gtfs_service_end': 'not-a-date'}, {'gtfs_service_end': '2025-01-01'},
])
def test_departures_do_not_guess_calendar_without_usable_graph_policy(tmp_path, overrides):
    t = tables(); t['calendar.txt'][0]['end_date'] = '20190101'
    publish(tmp_path, t); graph_calendar_policy(tmp_path, **overrides)
    response = api(tmp_path).get('/api/v1/stops/b/departures', params={
        'dummy': False, 'departure_at': '2026-11-11T00:50:00+03:00'})
    assert response.status_code == 422


def test_calendar_repair_preserves_exceptions_and_finite_valid_calendars(tmp_path):
    t = tables(); t['calendar.txt'][0]['end_date'] = '20190101'
    t['calendar_dates.txt'] = [{'service_id': 'service', 'date': '20261110', 'exception_type': '2'}]
    publish(tmp_path, t); graph_calendar_policy(tmp_path)
    params = {'dummy': False, 'departure_at': '2026-11-11T00:50:00+03:00'}
    items = api(tmp_path).get('/api/v1/stops/b/departures', params=params).json()['items']
    assert all(item['trip_id'] != 't1' for item in items)
    t = tables(); t['calendar.txt'][0]['end_date'] = '20261109'
    publish(tmp_path, t)
    assert api(tmp_path).get('/api/v1/stops/b/departures', params=params).status_code == 422
