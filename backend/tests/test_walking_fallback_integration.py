"""HTTP boundary tests with real OTP decoding and offline provider transports."""
import copy
import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.otp import OtpPlanner, decode_polyline

# Recorded night Ruchyi street geometry, 2026-11-13; copied here so research
# artifacts are not required by backend CI. Only missing GraphQL fields below
# are supplied from the recorded request, never fabricated street coordinates.
RECORDED_NODE = {'start': '2026-11-13T03:00:00+03:00', 'end': '2026-11-13T04:59:09+03:00', 'duration': 7149, 'walkDistance': 8749.03, 'legs': [{'mode': 'WALK', 'distance': 8749.03, 'legGeometry': {'points': 'sjfmJgt|wDDk@f@oH@IbAaOCQ@[?a@LCFeA`@aGVeERuCDKHkA@SFu@?EF{@LuB@y@Ao@Co@EiAE{@A]MeCw@sQGwAI{AUoFaBc_@Q}Di@iMImA?OCMCUGYOm@e@wAWoB?MCY?IAUIaBCUIMMC?IC]?MAO?GAYAOCmABu@J{@AQBGFKDG@EDEDKBCJNhAa@BA@GBQRqCFw@Dw@Fq@?KDe@@k@?w@?{DDi@h@}Gr@_J@KP_CRmCj@wHN}B`@AFw@LwA@S@M@O?ENQDo@?GH{@HgAGCd@}H@IAGDUJw@LyATwCLeBB]\\_FDCFq@Ba@@QDeADk@@O@S?C@UBQ?CKE\\{EJsAd@{ENgB@C`@qFFw@Fq@@@?C@K@K?CBUEa@Bo@JcBbAsNXyDPkCDg@?WPeC@QJmA@EFw@?EDk@R_CXsD@IJmAUYS[c@eAQ]Y_@E[CYEOEUPWT[FY@OBOF{@A_@@E@Y@K@M?MBYb@JTg@L@v@UPEPcDRwCPmCFBFcANaCJ}AAO|@wLXqDLmB@IJ_Br@mJr@iJ?E@I@I@S@KBI@ELsA?I@KLAP}BL}AB]?CB_@W}A@kAFy@JWBODS@GBK@KBGFHD?BC@EBQBeA@SD_AHkAd@oHb@oGh@}HLgBFeABSTeDBQD]NuB?KHq@Bk@ViE?IBs@@MBQ?K@C?Y@I@oA@_AC{Dq@Q?g@@e@?a@?m@AQ?OC]?U?_@?[B]@Y?_@?YAWAYAUI]QmAw@eGe@oDSs@Ma@MeFImAIe@Ik@OsAe@aCRyDTsA@kC}@aE_A}C{@_GEuDAeD@mDLm@T{Au@}BKUUy@@KMWYiAEa@Mw@{@uB{@_COc@I[Kk@g@iAm@oBG_AIw@_@wBGGMUMyBKqCKmCM}BOOBYMkAKoAAs@B_AH{@Du@F}ABmAA_ACcBG{CEkCg@e@OUYU]UGGGIIGo@o@i@i@[kDWcCOOKKKM@YHc@FSWwC_@uDyCyCcGiGTwALs@EEN}@I]Gi@?MEkABe@r@mF?[AaABa@B?EkABGDK?KAYAUAM?SAW?Sl@MREDA@?COOa@CIAK?I@M?IFQ^k@NQr@IRC', 'length': 401}}]}
REQUEST = {'points': [{'lat': 60.00303371629959, 'lon': 30.29806865775501}, {'lat': 60.00222602933025, 'lon': 30.437336450251067}], 'departure_at': '2026-11-13T03:00:00+03:00', 'traffic_mode': 'off'}


def empty(code='NO_TRANSIT_CONNECTION'):
    return {'data': {'planConnection': {'edges': [], 'routingErrors': [
        {'code': code, 'description': 'Recorded empty search'}]}}}


def walking():
    node = copy.deepcopy(RECORDED_NODE)
    node['numberOfTransfers'] = 0
    leg = node['legs'][0]
    leg.update(start={'scheduledTime': node['start']},
               end={'scheduledTime': node['end']}, transitLeg=False,
               **{'from': REQUEST['points'][0], 'to': REQUEST['points'][1]})
    return {'data': {'planConnection': {'edges': [{'node': node}], 'routingErrors': []}}}


def transit():
    return json.loads((Path(__file__).parents[1] / 'app/fixtures/otp/plan-response.json').read_text())


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Integration test attempted a real network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def post(tmp_path, replies, options=None, *, timeout=10, on_call=None):
    calls = []
    def handler(request):
        calls.append(request)
        if on_call:
            on_call(len(calls))
        reply = replies[len(calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json=reply)
    def forbidden(*args, **kwargs):
        pytest.fail('Walking must bypass traffic graph and paid road providers')
    class ForbiddenRoad:
        def __getattr__(self, name):
            return forbidden
    with httpx.Client(transport=httpx.MockTransport(handler)) as otp_client:
        planner = OtpPlanner('http://otp.invalid', client=otp_client, timeout=timeout)
        app = create_app(tmp_path, 'otp', transit_planner=planner,
                         road_provider=ForbiddenRoad(), dgis_road_provider=ForbiddenRoad(),
                         traffic_graph_loader=forbidden)
        with TestClient(app) as client:
            result = client.post('/api/v1/routes?dummy=false', json={**REQUEST, **(options or {})})
    assert result.status_code == 200, result.text
    return result.json(), calls


def variables(call):
    return json.loads(call.content)['variables']


@pytest.mark.parametrize('options', [{}, {'modes': None}, {'modes': ['bus']}])
def test_api_primary_always_offers_walking_without_forcing_extra_route(tmp_path, options):
    result, calls = post(tmp_path, [transit()], {**options, 'max_results': 1})
    assert len(calls) == 1
    modes = variables(calls[0])['modes']
    assert modes['direct'] == ['WALK'] and not modes.get('directOnly')
    assert len(modes['transit']['transit']) == (1 if options.get('modes') else 5)
    assert len(result['routes']) == 1
    assert any(leg['mode'] == 'bus' for leg in result['routes'][0]['legs'])


def test_api_empty_transit_fallback_preserves_recorded_street_path(tmp_path):
    result, calls = post(tmp_path, [empty(), walking()], {'modes': ['bus'], 'max_results': 1})
    assert len(calls) == 2
    assert variables(calls[1])['modes'] == {'direct': ['WALK'], 'directOnly': True}
    route, = result['routes']
    assert route['duration_seconds'] == 7149
    assert route['distance_meters'] == pytest.approx(8749.03)
    assert [leg['mode'] for leg in route['legs']] == ['walk']
    expected = [list(point) for point in decode_polyline(RECORDED_NODE['legs'][0]['legGeometry']['points'])]
    assert len(expected) > 100
    assert route['geometry']['coordinates'] == expected
    assert route['legs'][0]['geometry']['coordinates'] == expected
    assert any('NO_TRANSIT_CONNECTION' in warning for warning in result['warnings'])
    assert not any('found no route' in warning for warning in result['warnings'])
    assert result['sources']['traffic_providers'] == []


@pytest.mark.parametrize('limit', [1, 3, 10])
def test_api_walking_competes_under_shared_limit(tmp_path, limit):
    body = transit()
    body['data']['planConnection']['edges'] *= 12
    walk = walking()['data']['planConnection']['edges'][0]
    # Make the recorded walking itinerary rank first; only duration is changed.
    walk['node']['duration'] = 1
    body['data']['planConnection']['edges'].append(walk)
    result, calls = post(tmp_path, [body], {'max_results': limit})
    assert len(calls) == 1
    assert len(result['routes']) == limit
    assert all(leg['mode'] == 'walk' for leg in result['routes'][0]['legs'])


@pytest.mark.parametrize('second', [empty('WALK_UNREACHABLE'), httpx.ReadTimeout('walking timeout')])
def test_api_fallback_failure_keeps_original_routing_error(tmp_path, second):
    result, calls = post(tmp_path, [empty(), second], {'modes': ['bus']})
    assert len(calls) == 2 and result['routes'] == []
    assert any('NO_TRANSIT_CONNECTION' in warning for warning in result['warnings'])
    expected = 'WALK_UNREACHABLE' if isinstance(second, dict) else 'Walking fallback failed: planner_timeout'
    assert any(expected in warning for warning in result['warnings'])


def test_api_fallback_uses_remaining_deadline(tmp_path, monkeypatch):
    import app.otp as otp_module
    clock = [100.0]
    monkeypatch.setattr(otp_module.time, 'monotonic', lambda: clock[0])
    def advance(call_number):
        if call_number == 1:
            clock[0] += 6
    result, calls = post(tmp_path, [empty(), walking()], {'modes': ['bus']}, timeout=10, on_call=advance)
    assert result['routes'] and len(calls) == 2
    assert calls[0].extensions['timeout']['read'] <= 8
    assert calls[1].extensions['timeout']['read'] <= 4


@pytest.mark.parametrize('selection', [{'modes': ['walk']}, {'exclude_modes': ['bus', 'tram', 'trolleybus', 'metro', 'rail']}])
@pytest.mark.parametrize('reply', [walking(), empty()])
def test_api_pure_walking_is_one_call_without_paid_providers(tmp_path, selection, reply):
    result, calls = post(tmp_path, [reply], {**selection, 'traffic_mode': 'on'})
    assert len(calls) == 1
    assert variables(calls[0])['modes'] == {'direct': ['WALK'], 'directOnly': True}
    assert result['sources']['traffic_providers'] == []
    assert all(leg['mode'] == 'walk' for route in result['routes'] for leg in route['legs'])


def test_api_expired_deadline_does_not_start_walking_request(tmp_path, monkeypatch):
    import app.otp as otp_module
    clock = [100.0]
    monkeypatch.setattr(otp_module.time, 'monotonic', lambda: clock[0])
    def advance(_):
        clock[0] += 11
    result, calls = post(tmp_path, [empty()], {'modes': ['bus']}, timeout=10, on_call=advance)
    assert len(calls) == 1 and result['routes'] == []
    assert any('NO_TRANSIT_CONNECTION' in warning for warning in result['warnings'])
    assert any('time budget exhausted' in warning for warning in result['warnings'])
