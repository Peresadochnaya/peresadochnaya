import json
from pathlib import Path
import pytest
from app.traffic_data import RoadObservation
from app.yandex_router import parse_observations as parse_yandex
from app.dgis import parse_observations as parse_dgis
from app.yandex import ProviderError


def test_observation_fixed_expiry_and_validation():
    o=RoadObservation('a','yandex',((30,60),(30.01,60)),500,100,100,10000)
    assert o.expires_at==1900
    assert o.fresh(1899) and not o.fresh(1900)
    with pytest.raises(ValueError): RoadObservation('a','x',((30,60),(30.01,60)),10,-1,0)


def test_yandex_step_time_not_duplicated_and_realtime_only():
    data={'traffic_type':'realtime','route':{'legs':[{'status':'OK','steps':[{'mode':'driving','length':100,'duration':30,'polyline':{'points':[[60,30],[60,30.001],[60,30.002]]}}]}]}}
    result=parse_yandex(data,100)
    assert len(result)==1 and result[0].duration_seconds==30
    assert result[0].geometry[0]==(30,60)
    data['traffic_type']='forecast'
    with pytest.raises(ProviderError): parse_yandex(data,100)


def test_dgis_whole_path_and_all_alternatives_recorded():
    path=Path(__file__).resolve().parents[2]/'research/traffic-correction/pionerskaya-rodeo-drive/2gis-response.json'
    data=json.loads(path.read_text())
    observations=parse_dgis(data,100)
    assert {o.alternative_index for o in observations}==set(range(len(data['result'])))
    assert sum(o.duration_seconds for o in observations)==sum(m['outcoming_path']['duration'] for item in data['result'] for m in item['maneuvers'] if m.get('outcoming_path'))


def test_dgis_geometry_parts_do_not_duplicate_duration():
    path={'distance':100,'duration':40,'geometry':[{'selection':'LINESTRING(30 60,30.001 60)'},{'selection':'LINESTRING(30.001 60,30.002 60)'}]}
    data={'status':'OK','type':'result','result':[{'maneuvers':[{'outcoming_path':path},{}]}]}
    result=parse_dgis(data,0)
    assert len(result)==1 and result[0].duration_seconds==40 and len(result[0].geometry)==3


def test_provider_one_call_current_parameters_and_budget(monkeypatch):
    import httpx
    from app.dgis import DgisRouter
    from app.yandex_router import YandexRouter
    calls=[]
    def handler(request):
        calls.append(request)
        if '2gis' in request.url.host:
            body=json.loads(request.content)
            assert body['alternative']>0 and body['traffic_mode']=='jam' and body['route_mode']=='fastest'
            assert 'utc' not in body
            return httpx.Response(200,json={'status':'OK','type':'result','result':[{'maneuvers':[{'outcoming_path':{'distance':100,'duration':40,'geometry':[{'selection':'LINESTRING(30 60,30.002 60)'}]}}]}]})
        assert 'departure_time' not in request.url.params and 'optimize' not in request.url.params
        return httpx.Response(200,json={'traffic_type':'realtime','route':{'legs':[{'status':'OK','steps':[{'mode':'driving','length':100,'duration':40,'polyline':{'points':[[60,30],[60,30.002]]}}]}]}})
    monkeypatch.setenv('DGIS_API_KEY','offline-test')
    monkeypatch.setenv('YANDEX_ROUTER_API_KEY','offline-test')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for provider in (DgisRouter(client),YandexRouter(client)):
            assert len(provider.estimate_observations([(30,60),(30.002,60)],timeout_seconds=.5))==1
            assert calls[-1].extensions['timeout']['read']==.5
    assert len(calls)==2

@pytest.mark.parametrize('field,value',[('duration',float('nan')),('distance',-1)])
def test_dgis_rejects_invalid_measurements(field,value):
    path={'distance':100,'duration':40,'geometry':[{'selection':'LINESTRING(30 60,30.002 60)'}]}
    path[field]=value
    with pytest.raises(ProviderError): parse_dgis({'status':'OK','type':'result','result':[{'maneuvers':[{'outcoming_path':path}]}]},0)


def test_dgis_malformed_partial_path_rejects_response():
    path={'distance':100,'duration':40,'geometry':[{'selection':'LINESTRING(30 60,30.002 60)'},{'selection':'LINESTRING(NaN 60,30.003 60)'}]}
    with pytest.raises(ProviderError): parse_dgis({'status':'OK','type':'result','result':[{'maneuvers':[{'outcoming_path':path}]}]},0)

@pytest.mark.parametrize('item',[None,{'maneuvers':[None]},{'maneuvers':[{'outcoming_path':{'geometry':[None]}}]}])
def test_dgis_null_response_entries_raise_provider_error(item):
    with pytest.raises(ProviderError): parse_dgis({'status':'OK','type':'result','result':[item]},0)
