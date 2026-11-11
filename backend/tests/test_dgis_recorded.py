import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dgis import DgisAddresses, DgisRouter
from app.main import create_app


@pytest.mark.parametrize('name',['suggest','geocode','reverse','road-route'])
def test_saved_2gis_examples_replay_offline(tmp_path,monkeypatch,name):
    def forbidden(*args,**kwargs):
        raise AssertionError('Recorded 2GIS tests must not use live network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setenv('DGIS_API_KEY','offline-fixture-key')
    path=Path(__file__).parents[1]/'app/fixtures/dgis/live-example-2026-11-11.json'
    report=json.loads(path.read_text());assert report['upstream_calls']==4
    case=report['cases'][name];calls=[]
    def handler(request):
        calls.append(request)
        assert str(request.url.copy_remove_param('key'))==case['upstream_url']
        return httpx.Response(case['upstream_status'],json=case['upstream_response'])
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client=TestClient(create_app(tmp_path,dgis_address_provider=DgisAddresses(http),dgis_road_provider=DgisRouter(http)))
        req=case['request'];response=client.request(req['method'],req['path'],params=req['params'],json=req['body'])
    assert response.status_code==200 and len(calls)==1
    actual=response.json();expected=dict(case['api_response'])
    if name=='road-route':
        actual.pop('calculated_at');expected.pop('calculated_at')
    assert actual==expected
