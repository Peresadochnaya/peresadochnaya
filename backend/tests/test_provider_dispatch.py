import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dgis import DgisAddresses, DgisRouter
from app.gtfs_store import demo_stops
from app.main import create_app
from app.schemas import RouteRequest
from app.yandex import YandexAddresses
from app.yandex_router import YandexRouter


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Provider tests must never use live API quota")
    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.mark.parametrize("provider", ["yandex", "2gis"])
def test_all_dummy_endpoints_keep_selected_provider_and_share_gtfs_points(tmp_path, provider):
    client = TestClient(create_app(tmp_path))
    stops = {s['stop_id']:s for s in demo_stops()}
    data = client.get('/api/v1/suggest',params={'q':'демо','provider':provider}).json()
    assert data['mode']=='dummy' and data['provider']==provider
    item=data['items'][0]
    stop_id=item['uri'].split(':stop:',1)[1]
    assert item['title']==stops[stop_id]['name']
    geo=client.get('/api/v1/geocode',params={'uri':item['uri'],'provider':provider}).json()
    assert geo['mode']=='dummy' and geo['provider']==provider
    point=geo['items'][0]
    assert (point['lat'],point['lon'])==(stops[stop_id]['lat'],stops[stop_id]['lon'])
    reverse=client.get('/api/v1/reverse',params={'lat':point['lat'],'lon':point['lon'],'provider':provider}).json()
    assert reverse['provider']==provider and reverse['items'][0]['label']==stops[stop_id]['name']
    body={'points':[{'lat':point['lat'],'lon':point['lon']},{'lat':60,'lon':30}]}
    road=client.post('/api/v1/road-route',params={'provider':provider},json=body).json()
    assert road['provider']==provider and road['mode']=='dummy'
    assert road['route']['geometry']['coordinates'][0]==[point['lon'],point['lat']]
    # Real mode must not accidentally forward dummy tokens to any provider.
    assert client.get('/api/v1/geocode',params={'uri':item['uri'],'provider':provider,'dummy':False}).status_code==422


def test_dgis_selection_never_falls_back_to_yandex(tmp_path,monkeypatch):
    monkeypatch.delenv('DGIS_API_KEY',raising=False)
    def forbidden(request):
        pytest.fail('Unexpected fallback request to Yandex')
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        client=TestClient(create_app(tmp_path,address_provider=YandexAddresses(http),road_provider=YandexRouter(http)))
        for path,params in [('suggest',{'q':'Ручьи'}),('geocode',{'q':'Ручьи'}),('reverse',{'lat':60,'lon':30})]:
            r=client.get('/api/v1/'+path,params={**params,'provider':'2gis','dummy':False})
            assert r.status_code==503 and r.json()['error']['code'].startswith('dgis_')
        r=client.post('/api/v1/road-route',params={'provider':'2gis','dummy':False},json={'points':[{'lat':60,'lon':30},{'lat':60.1,'lon':30.1}]})
        assert r.status_code==503 and r.json()['error']['code'].startswith('dgis_')


def test_provider_validation_and_token_isolation(tmp_path):
    c=TestClient(create_app(tmp_path))
    assert c.get('/api/v1/suggest',params={'q':'Ручьи','provider':'tomtom'}).status_code==422
    for provider,uri in [('2gis','ymapsbm1://geo?data=x'),('yandex','dgis:123'),('2gis','dummy:yandex:stop:1'),('yandex','dummy:2gis:stop:1')]:
        r=c.get('/api/v1/geocode',params={'uri':uri,'provider':provider,'dummy':False})
        assert r.status_code==422


def test_route_preferences_are_contract_only_and_real_search_remains_unavailable(tmp_path):
    body={'points':[{'lat':60,'lon':30},{'lat':60.1,'lon':30.1}],
          'departure_at':'2099-11-11T10:00:00+03:00','modes':['bus','metro'],
          'providers':['otp'],'walk_speed':'slow','less_walking':True,
          'passenger':{'ticket_profile':'unlimited_surface','uncovered_payment':'podorozhnik_wallet'}}
    client=TestClient(create_app(tmp_path))
    response=client.post('/api/v1/routes?dummy=false',json=body)
    assert response.status_code==503 and response.json()['error']['code']=='planner_unavailable'
    data=client.post('/api/v1/routes',json=body).json()
    assert data['mode']=='dummy' and data['passenger']==body['passenger']
    for key,value in [('modes',[]),('modes',['boat']),('providers',[]),('providers',['otp','otp']),('walk_speed','custom')]:
        assert client.post('/api/v1/routes',json={**body,key:value}).status_code==422
    assert RouteRequest.model_validate({k:v for k,v in body.items() if k!='providers'}).providers==['otp']


def test_five_pass_profiles_and_configurable_uncovered_payment(tmp_path):
    client=TestClient(create_app(tmp_path))
    profiles={p['id']:p for p in client.get('/api/v1/ticket-profiles').json()}
    assert profiles['podorozhnik_wallet']['transfer_window_seconds']==3600
    assert profiles['unlimited_surface']['covered_modes']==['bus','tram','trolleybus']
    assert profiles['unlimited_metro']['covered_modes']==['metro']
    assert 'rail' not in profiles['unlimited_all']['covered_modes']
    assert all(p['coverage_status']=='not_implemented' for p in profiles.values())
    for payment in ['bank_card','podorozhnik_wallet']:
        body={'points':[{'lat':60,'lon':30},{'lat':60.1,'lon':30.1}], 'departure_at':'2099-11-11T10:00:00Z',
              'passenger':{'ticket_profile':'unlimited_metro','uncovered_payment':payment}}
        assert client.post('/api/v1/routes',json=body).json()['passenger']==body['passenger']
