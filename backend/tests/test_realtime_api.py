import httpx
from fastapi.testclient import TestClient
from app.main import create_app
from app import realtime


def test_raw_feed_proxies_binary_and_reports_unavailable(tmp_path,monkeypatch):
    def response(path,params=None):
        return httpx.Response(200,content=b'protobuf',headers={'x-feed-timestamp':'123','x-feed-stale':'true','cache-control':'no-store'})
    monkeypatch.setattr(realtime,'request',response)
    with TestClient(create_app(tmp_path)) as client:
        r=client.get('/api/v1/raw_feed')
        assert r.status_code==200 and r.content==b'protobuf'
        assert r.headers['content-type']=='application/octet-stream' and r.headers['x-feed-stale']=='true'
        def fail(*a,**kw): raise httpx.ConnectError('offline')
        monkeypatch.setattr(realtime,'request',fail)
        assert client.get('/api/v1/raw_feed').status_code==503


def test_real_vehicle_mode_uses_separate_service_without_generator(tmp_path,monkeypatch):
    monkeypatch.setenv('REALTIME_URL','http://realtime:8001')
    calls=[]
    def response(path,params=None):
        calls.append((path,params))
        return httpx.Response(200,json={'mode':'realtime','items':[{'vehicle_id':'v','route_id':'r',
            'trip_id':'t','lat':60,'lon':30,'heading':90,'mode':'bus','timestamp':'2026-11-12T12:00:00Z'}],
            'updated_at':'2026-11-12T12:00:00Z','stale':False,'warnings':[]})
    monkeypatch.setattr(realtime,'request',response)
    with TestClient(create_app(tmp_path)) as client:
        r=client.get('/api/v1/vehicles',params={'bbox':'29,59,31,61','dummy':'false','modes':'bus, tram'})
    assert r.status_code==200 and r.json()['items'][0]['heading']==90
    assert calls==[('/api/v1/vehicles',{'bbox':'29,59,31,61','limit':10000,'modes':'bus,tram'})]
