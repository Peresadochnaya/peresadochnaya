"""Offline end-to-end engine examples with independent OTP and GTFS stand-ins."""
from datetime import datetime, timedelta, timezone
import socket

import pytest

from app.schemas import (Leg, LineString, Place, Route, RouteRequest, RouteProvenance,
    RoutesResponse, RoutesResponseSources, TransitLine)
from app.traffic_data import RoadObservation, SegmentBaseline
from app.traffic_engine import TrafficEngine, coverage
from app.traffic_geometry import length
from app.traffic_timing import classify_coverage, transfer_decision

START = datetime(2026, 11, 12, 9, tzinfo=timezone.utc)
LINE = ((30.0, 60.0), (30.02, 60.0))
NOW = START.timestamp()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Traffic integration attempted live network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def request(**updates):
    return RouteRequest(points=[{'lat':60,'lon':30},{'lat':60,'lon':30.02}],
        departure_at=START, traffic_mode='on', modes=['bus'], **updates)


def leg(trip='first', start=0, duration=480, mode='bus', origin='a', destination='b', distance=1000):
    return Leg(mode=mode, origin=Place(lat=60,lon=30,name=origin,stop_id=origin),
        destination=Place(lat=60,lon=30.02,name=destination,stop_id=destination),
        start_at=START+timedelta(seconds=start),end_at=START+timedelta(seconds=start+duration),
        duration_seconds=duration,original_duration_seconds=duration,duration_source='schedule',
        geometry=LineString(coordinates=list(LINE)),distance_meters=distance,
        line=TransitLine(short_name='1',route_id='line',trip_id=trip) if mode not in ['walk','wait'] else None)


def route(id='r', legs=None):
    legs = legs or [leg()]
    return Route(id=id,name=id,distance_meters=sum(x.distance_meters for x in legs),
        geometry=LineString(coordinates=list(LINE)),start_at=legs[0].start_at,end_at=legs[-1].end_at,
        duration_seconds=int((legs[-1].end_at-legs[0].start_at).total_seconds()),
        transfers=max(0,sum(x.line is not None for x in legs)-1),legs=legs,
        provenance=RouteProvenance(candidate_providers=['otp'],traffic_providers=[]))


def response(routes):
    return RoutesResponse(mode='real',sources=RoutesResponseSources(candidate_providers=['otp'],
        traffic_providers=[],realtime='unavailable'),passenger=request().passenger,routes=routes,
        gtfs_version='v1',graph_version='g1',calculated_at=START,warnings=[])


class Planner:
    def __init__(self, routes, tails=None):
        self.routes=routes; self.tails=tails; self.calls=[]
    def plan(self, req, **kwargs):
        self.calls.append((req.model_copy(deep=True), kwargs))
        if len(self.calls)>1:
            if isinstance(self.tails, Exception): raise self.tails
            return response(self.tails or [])
        return response(self.routes).model_copy(deep=True)


class Graph:
    def __init__(self, *, frequency=False, baselines=None):
        self.frequency=frequency; self.baselines=baselines
    def trip_segments(self, trip_id, **kwargs):
        if self.baselines is not None: return self.baselines.get(trip_id, [])
        return [SegmentBaseline('feed','v1',trip_id,'a','b',1,2,LINE,480,
            road_status='unknown')] if trip_id=='first' else []
    def service_info(self, trip_id, **kwargs):
        return {'service_kind':'frequency' if self.frequency and trip_id!='first' else 'scheduled',
            'frequency_exact_times':0 if self.frequency and trip_id!='first' else None,
            'headway_seconds':300 if self.frequency and trip_id!='first' else None,
            'wait_model':'otp_model' if self.frequency and trip_id!='first' else 'scheduled_departure'}


def road(seconds=600, geometry=LINE):
    return RoadObservation('road','yandex',geometry,length(geometry),seconds,NOW)


def engine(planner, graph=None, observations=None, clock=lambda:0):
    return TrafficEngine(planner, graph_loader=lambda **kwargs:graph,
        clock=clock,wall_clock=lambda:NOW,
        collector=lambda candidates, graph, deadline:(observations or [], []))


def assert_timeline(value):
    assert value.duration_seconds == sum(x.duration_seconds for x in value.legs)
    assert value.duration_seconds == int((value.end_at-value.start_at).total_seconds())
    for left,right in zip(value.legs,value.legs[1:]): assert left.end_at == right.start_at


@pytest.mark.parametrize('eligible,covered,expected', [(1000,799.99,'incomplete'),
    (1000,800,'sufficient'),(1000,1000,'sufficient'),(0,0,'not_applicable')])
def test_quality_threshold_is_distance_based_with_null_non_applicable(eligible,covered,expected):
    assert classify_coverage(eligible,covered) == expected
    details=coverage(eligible,covered)
    assert details.usable_coverage_ratio == (covered/eligible if eligible else None)


def test_unverified_tail_overrides_even_full_road_coverage():
    assert classify_coverage(1000,1000,True) == 'incomplete'


@pytest.mark.parametrize('frequency,delayed,walk,boarding,reachable,replan', [
    (False,True,60,600,True,False),(False,True,120,600,True,False),
    (False,True,121,600,False,True),(True,True,60,600,True,True),
    (True,False,60,600,True,False),(True,False,121,600,False,True)])
def test_transfer_rules_preserve_exact_but_refresh_delayed_frequency(frequency,delayed,walk,boarding,reachable,replan):
    result=transfer_decision(START+timedelta(seconds=480),START+timedelta(seconds=boarding),walk,frequency,delayed)
    assert result.reachable is reachable and result.replan is replan
    assert result.slack_seconds == boarding-480-walk


def test_single_leg_positive_correction_preserves_departure_and_full_timeline():
    original=route(); planner=Planner([original])
    result=engine(planner,Graph(),[road()]).plan(request(),gtfs_version='v1',graph_version='g1')
    corrected=result.routes[0]
    assert corrected.start_at == START
    assert corrected.duration_seconds == 600
    assert corrected.legs[0].original_duration_seconds == 480
    assert corrected.provenance.traffic_providers == ['yandex']
    assert corrected.debug.evaluation_group == 'sufficient'
    assert original.duration_seconds == 480
    assert_timeline(corrected)
    assert 0 < planner.calls[0][1]['timeout_seconds'] <= 15
    assert result.debug.cache_ttl_seconds == 1800 and result.debug.time_budget_seconds == 15


def test_exact_transfer_absorbs_delay_in_wait_and_keeps_boarding():
    legs=[leg(),leg(mode='walk',start=480,duration=60,origin='b',destination='platform',distance=70),
        leg('second',start=900,duration=300,origin='platform',destination='c')]
    planner=Planner([route(legs=legs)])
    result=engine(planner,Graph(),[road()]).plan(request())
    value=result.routes[0]
    assert len(planner.calls) == 1
    assert next(x for x in value.legs if x.line and x.line.trip_id=='second').start_at == START+timedelta(seconds=900)
    assert [x.duration_seconds for x in value.legs if x.mode=='wait'] == [240]
    assert value.end_at == START+timedelta(seconds=1200)
    transfer=value.debug.transfers[0]
    assert transfer.status=='feasible' and transfer.resolution=='kept'
    assert transfer.corrected_arrival_at==START+timedelta(seconds=600)
    assert transfer.minimum_transfer_seconds==60
    assert transfer.remaining_slack_seconds==240
    assert transfer.scheduled_departure_at==START+timedelta(seconds=900)
    assert value.legs[transfer.to_leg_index].line.trip_id=='second'
    assert_timeline(value)


def test_delayed_reachable_frequency_tail_requires_recovery_and_can_fallback_unverified():
    legs=[leg(),leg('second',start=900,duration=300,origin='b',destination='c')]
    planner=Planner([route(legs=legs)],tails=TimeoutError('deadline'))
    result=engine(planner,Graph(frequency=True),[road()]).plan(request())
    assert len(planner.calls) == 2
    value=result.routes[0]
    assert value.debug.evaluation_group == 'incomplete'
    assert value.debug.tail_replanning_status == 'unverified_fallback'
    assert value.debug.warnings
    event=value.debug.transfers[0]
    assert event.modeled_boarding_reachable is True
    assert event.scheduled_departure_at is None
    assert event.resolution == 'kept_unverified'
    assert planner.calls[1][0].departure_at == START+timedelta(seconds=600)
    assert planner.calls[1][1]['origin_stop_id'] == 'b'
    assert_timeline(value)


def test_unreachable_unrepaired_candidate_excluded_with_full_original():
    original=route(legs=[leg(),leg('second',start=540,duration=300,origin='b',destination='c')])
    snapshot=original.model_dump()
    planner=Planner([original],tails=TimeoutError('deadline'))
    result=engine(planner,Graph(frequency=True),[road()]).plan(request())
    assert result.routes == []
    assert result.debug.excluded_count == 1
    assert result.debug.excluded_candidates[0].original_route.model_dump() == snapshot
    assert original.model_dump() == snapshot


def test_successful_tail_replaces_old_walk_wait_and_rides_and_preserves_prefix():
    original=route(legs=[leg(),leg(mode='walk',start=480,duration=60,origin='b',destination='old',distance=70),
        leg('second',start=540,duration=300,origin='old',destination='c')])
    replacement=route('tail',legs=[leg(mode='walk',start=600,duration=30,origin='b',destination='new',distance=35),
        leg('replacement',start=660,duration=200,origin='new',destination='c')])
    planner=Planner([original],tails=[replacement])
    result=engine(planner,Graph(),[road()]).plan(request(max_transfers=1,less_walking=True,walk_speed='fast'))
    value=result.routes[0]
    assert [x.line.trip_id for x in value.legs if x.line] == ['first','replacement']
    assert not any(x.destination.stop_id=='old' for x in value.legs)
    assert value.legs[0].duration_seconds == 600
    assert value.end_at == START+timedelta(seconds=860)
    tail_req=planner.calls[1][0]
    assert tail_req.max_transfers == 0 and tail_req.less_walking is True and tail_req.walk_speed=='fast'
    assert tail_req.modes == ['bus']
    assert value.debug.evaluation_group=='incomplete'  # replacement roads have no observations/baseline
    assert_timeline(value)


def test_shared_result_limit_prefers_sufficient_over_faster_partial_and_zero_eligible():
    partial=route('fast-partial',legs=[leg('uncovered',duration=200)])
    covered=route('slow-covered')
    metro=route('metro',legs=[leg('metro',duration=700,mode='metro')])
    result=engine(Planner([partial,covered,metro]),Graph(),[road()]).plan(request(max_results=2))
    assert [x.id for x in result.routes] == ['slow-covered','metro']
    assert result.routes[1].debug.evaluation_group=='not_applicable'
    assert result.routes[1].debug.coverage.coverage_ratio is None
    assert result.debug.candidate_count == 3 and result.debug.returned_count == 2


def test_observation_debug_distinguishes_received_from_applied_when_added_zero():
    result=engine(Planner([route()]),Graph(),[road(400)]).plan(request())
    value=result.routes[0]
    assert value.debug.evaluation_group=='sufficient'
    assert value.provenance.traffic_providers == []
    detail=value.legs[0].debug.segments[0]
    assert detail.observations  # received observations must stay visible even if no correction
    assert detail.applied_provider is None
    assert detail.observations[0].driving_duration_seconds == 400
    assert detail.observations[0].observed_at == START
    assert detail.observations[0].expires_at == START+timedelta(seconds=1800)


def test_initial_otp_wait_is_preserved_before_first_scheduled_departure():
    original=route(legs=[leg(mode='wait',start=0,duration=120,distance=0),
        leg(start=120,duration=480)])
    result=engine(Planner([original]),Graph(),[road()]).plan(request())
    value=result.routes[0]
    assert value.start_at == START
    assert value.legs[0].mode == 'wait' and value.legs[0].duration_seconds==120
    assert next(x for x in value.legs if x.line).start_at == START+timedelta(seconds=120)
    assert_timeline(value)


def test_successful_frequency_repair_does_not_request_same_boundary_again():
    original=route(legs=[leg(),leg('second',start=900,duration=300,origin='b',destination='c')])
    replacement=route('tail',legs=[leg('replacement',start=750,duration=250,origin='b',destination='c')])
    planner=Planner([original],tails=[replacement])
    result=engine(planner,Graph(frequency=True),[road()]).plan(request())
    assert len(planner.calls)==2
    value=result.routes[0]
    assert value.debug.tail_replanning_status=='complete'
    assert all(event.resolution=='otp_replanned' for event in value.debug.transfers)
    assert_timeline(value)


def test_initial_otp_exhausts_deadline_so_no_recovery_work_is_started():
    clock=[0.]
    original=route(legs=[leg(),leg('second',start=900,duration=300,origin='b',destination='c')])
    planner=Planner([original])
    actual_plan=planner.plan
    def consumes_deadline(req, **kwargs):
        result=actual_plan(req,**kwargs); clock[0]=15.
        return result
    planner.plan=consumes_deadline
    result=engine(planner,Graph(frequency=True),[road()],clock=lambda:clock[0]).plan(request())
    assert len(planner.calls)==1
    assert result.debug.deadline_reached is True
    assert result.debug.otp_replanning_count==0


def test_missing_geometry_part_remains_eligible_in_distance_denominator():
    a=SegmentBaseline('feed','v1','first','a','middle',1,2,LINE,240,road_status='unknown')
    b=SegmentBaseline('feed','v1','first','middle','b',2,3,(),240,road_status='unknown')
    original=route(legs=[leg(distance=length(LINE)*2)])
    value=engine(Planner([original]),Graph(baselines={'first':[a,b]}),[road()]).plan(request()).routes[0]
    assert value.debug.evaluation_group=='incomplete'
    assert value.debug.coverage.eligible_distance_meters >= length(LINE)*2-0.01
    assert value.debug.coverage.usable_coverage_ratio == pytest.approx(.5)


def test_no_road_data_never_claims_free_flow_or_sufficient_coverage():
    value=engine(Planner([route()]),Graph(),[]).plan(request()).routes[0]
    assert value.duration_seconds==480
    assert value.debug.evaluation_group=='incomplete'
    assert value.debug.coverage.usable_coverage_ratio==0
    assert value.provenance.traffic_providers==[]


def test_grouping_ties_use_arrival_then_transfers_then_walking():
    # All routes have no applicable road distance, so ranking differences cannot
    # be hidden by uncertain correction groups.
    later=route('later',legs=[leg('m',mode='metro',duration=900)])
    walking=route('walking',legs=[leg(mode='walk',duration=100,distance=100),
        leg('m',mode='metro',start=100,duration=500)])
    direct=route('direct',legs=[leg('m',mode='metro',duration=600)])
    changing=route('changing',legs=[leg('m1',mode='metro',duration=300),
        leg('m2',mode='metro',start=300,duration=300)])
    result=engine(Planner([later,changing,walking,direct]),Graph(),[]).plan(request())
    assert [x.id for x in result.routes]==['direct','walking','changing','later']
    for value in result.routes: assert_timeline(value)


def test_api_dummy_and_traffic_off_never_call_road_providers(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    class ForbiddenRoad:
        def estimate_observations(self,*args,**kwargs):
            pytest.fail('Dummy/off search called paid provider')
    planner=Planner([route()])
    app=create_app(tmp_path,'otp',transit_planner=planner,
        road_provider=ForbiddenRoad(),dgis_road_provider=ForbiddenRoad())
    with TestClient(app) as client:
        demo=client.post('/api/v1/routes',json=request().model_dump(mode='json'))
        assert demo.status_code==200 and demo.json()['mode']=='dummy'
        assert planner.calls==[]
        off=request().model_copy(update={'traffic_mode':'off'})
        real=client.post('/api/v1/routes?dummy=false',json=off.model_dump(mode='json'))
        assert real.status_code==200 and real.json()['mode']=='real'
        assert real.json()['debug'] is None
        assert len(planner.calls)==1


def test_api_production_composition_reads_active_index_and_reuses_road_cache(tmp_path,monkeypatch):
    import json
    import httpx
    from fastapi.testclient import TestClient
    from app import gtfs_index
    import app.main as main
    from app.traffic_cache import TrafficCache
    from app.traffic_planner import ObservationCollector
    from app.yandex_router import YandexRouter
    from app.dgis import DgisRouter
    from test_gtfs_api import publish
    digest,_=publish(tmp_path)
    monkeypatch.setenv('GTFS_INDEX_DIR',str(tmp_path/'indexes'))
    gtfs_index.warm(tmp_path)
    (tmp_path/'otp').mkdir()
    (tmp_path/'otp/current.json').write_text(json.dumps({'version':'g1','gtfs_source_sha256':digest}))
    secret='OFFLINE_SENTINEL_NEVER_RETURN'
    monkeypatch.setenv('YANDEX_ROUTER_API_KEY',secret)
    monkeypatch.setenv('DGIS_API_KEY',secret)
    cache=TrafficCache()
    monkeypatch.setattr(main,'ObservationCollector',lambda roads,dgis:ObservationCollector(roads,dgis,cache=cache))
    calls=[]
    def dgis_handler(req):
        calls.append(('2gis',req))
        # Valid empty observation set leaves coverage for Yandex, rather than
        # manufacturing a provider failure to force the second source.
        return httpx.Response(200,json={'status':'ROUTE_NOT_FOUND','type':'result','result':[]})
    def yandex_handler(req):
        calls.append(('yandex',req))
        return httpx.Response(200,json={'traffic_type':'realtime','route':{'legs':[{'status':'OK','steps':[
            {'mode':'driving','length':13000,'duration':1500,
             'polyline':{'points':[[59.9,30.1],[60.0,30.2]]}}]}]}})
    trip_leg=leg('t1',duration=1200)
    trip_leg.origin=Place(lat=59.9,lon=30.1,name='a',stop_id='a')
    trip_leg.destination=Place(lat=60,lon=30.2,name='b',stop_id='b')
    trip_leg.geometry=LineString(coordinates=[(30.1,59.9),(30.2,60)])
    planner=Planner([route(legs=[trip_leg])])
    # Every separate user search receives candidates; Planner's tail convenience
    # behavior is disabled here so this is not an engine stub response.
    planner.plan=lambda req,**kwargs:response([route(legs=[trip_leg.model_copy(deep=True)])])
    with httpx.Client(transport=httpx.MockTransport(yandex_handler)) as yc, \
         httpx.Client(transport=httpx.MockTransport(dgis_handler)) as dc:
        app=main.create_app(tmp_path,'otp',transit_planner=planner,
            road_provider=YandexRouter(yc),dgis_road_provider=DgisRouter(dc))
        with TestClient(app) as client:
            first=client.post('/api/v1/routes?dummy=false',json=request().model_dump(mode='json'))
            assert first.status_code==200,first.text
            body=first.json()
            assert body['routes'][0]['duration_seconds']==1500
            assert body['routes'][0]['debug']['evaluation_group']=='sufficient'
            assert body['routes'][0]['legs'][0]['debug']['segments'][0]['baseline_gtfs_version']==digest
            assert body['debug']['graph_version']=='g1'
            assert body['routes'][0]['provenance']['traffic_providers']==['yandex']
            assert secret not in first.text
            assert [name for name,req in calls]==['2gis','yandex']
            second=client.post('/api/v1/routes?dummy=false',json=request().model_dump(mode='json'))
            assert second.status_code==200 and second.json()['routes'][0]['duration_seconds']==1500
            assert [name for name,req in calls]==['2gis','yandex']
            assert all(call['billable_units']==0 for call in second.json()['debug']['provider_calls'] if call['provider']!='otp')
    dgis_req=calls[0][1]
    body=json.loads(dgis_req.content)
    assert body['transport']=='driving' and body['route_mode']=='fastest' and body['traffic_mode']=='jam'
    assert body['output']=='detailed' and 'utc' not in body
    yandex_req=calls[1][1]
    assert 'departure_time' not in yandex_req.url.params and 'optimize' not in yandex_req.url.params
    assert len(yandex_req.url.params['waypoints'].split('|'))<=50


def test_api_real_planner_failure_never_falls_back_to_dummy(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.yandex import ProviderError
    class BrokenPlanner:
        def plan(self,*args,**kwargs):
            raise ProviderError(504,'otp_timeout','Offline planner timeout')
    with TestClient(create_app(tmp_path,'otp',transit_planner=BrokenPlanner())) as client:
        result=client.post('/api/v1/routes?dummy=false',json=request().model_dump(mode='json'))
    assert result.status_code==504
    assert result.json()['error']['code']=='otp_timeout'
    assert 'routes' not in result.json()


def test_provider_errors_are_not_retried_and_raw_exception_secret_is_not_debugged():
    import time
    from app.traffic_planner import ObservationCollector
    from app.traffic_cache import TrafficCache
    secret='SENTINEL_PRIVATE_KEY'
    class BrokenRoad:
        def __init__(self): self.calls=[]
        def estimate_observations(self,points,timeout_seconds):
            self.calls.append((points,timeout_seconds))
            raise RuntimeError('https://private.test/?apikey='+secret)
    first,second=BrokenRoad(),BrokenRoad()
    collector=ObservationCollector(second,first,cache=TrafficCache(),wall_clock=lambda:NOW)
    result=TrafficEngine(Planner([route()]),graph_loader=lambda **kwargs:Graph(),
        collector=collector,wall_clock=lambda:NOW).plan(request())
    assert len(first.calls)==len(second.calls)==1
    assert all(0 < calls[0][1] <= 15 for calls in [first.calls,second.calls])
    assert result.routes[0].debug.evaluation_group=='incomplete'
    assert result.routes[0].duration_seconds==480
    calls=[call for call in result.debug.provider_calls if call.provider!='otp']
    assert len(calls)==2 and all(call.status=='failed' for call in calls)
    assert secret not in result.model_dump_json()
    assert 'private.test' not in result.model_dump_json()


def test_blocking_initial_otp_returns_at_search_deadline(monkeypatch):
    import time
    import threading
    import app.traffic_engine as implementation
    monkeypatch.setattr(implementation,'SEARCH_SECONDS',.15)
    release=threading.Event(); started=threading.Event()
    class SlowPlanner:
        def plan(self,req,**kwargs):
            started.set()
            release.wait(.5)
            return response([route()])
    start=time.monotonic()
    try:
        from app.yandex import ProviderError
        with pytest.raises(ProviderError) as failed:
            TrafficEngine(SlowPlanner()).plan(request())
        assert failed.value.status==504
        assert failed.value.code=='planner_timeout'
        assert time.monotonic()-start < .35
        assert started.is_set()
    finally:
        release.set()



def test_api_search_deadline_returns_504_without_synthetic_route(tmp_path,monkeypatch):
    import threading
    import app.traffic_engine as implementation
    from fastapi.testclient import TestClient
    from app.main import create_app
    release=threading.Event()
    monkeypatch.setattr(implementation,'SEARCH_SECONDS',.15)
    class BlockingPlanner:
        def plan(self,req,**kwargs):
            release.wait(.5)
            return response([route()])
    try:
        with TestClient(create_app(tmp_path,'otp',transit_planner=BlockingPlanner())) as client:
            result=client.post('/api/v1/routes?dummy=false',json=request().model_dump(mode='json'))
        assert result.status_code==504
        assert 'routes' not in result.json()
        assert result.json()['error']['code']
    finally:
        release.set()


def test_every_unchanged_transfer_has_diagnostics_and_unknown_requirements_are_visible():
    legs=[leg(duration=480),leg('second',start=600,duration=300,origin='b',destination='c'),
        leg('third',start=1000,duration=300,origin='c',destination='d')]
    value=engine(Planner([route(legs=legs)]),Graph(),[]).plan(request()).routes[0]
    assert len(value.debug.transfers)==2
    assert all(event.status=='feasible' and event.resolution=='kept' for event in value.debug.transfers)
    assert [event.remaining_slack_seconds for event in value.debug.transfers]==[120,100]
    unknown=TrafficEngine(Planner([route(legs=legs)]),graph_loader=lambda **kw:Graph(),
        collector=lambda *args:([],[]),transfer_slack_loader=lambda:None,clock=lambda:0,wall_clock=lambda:NOW).plan(request()).routes[0]
    assert len(unknown.debug.transfers)==2
    assert all(event.status=='unknown' for event in unknown.debug.transfers)
    assert unknown.debug.transfer_status=='unknown'
    assert unknown.debug.evaluation_group=='incomplete'
    assert any('transfer requirements' in warning for warning in unknown.debug.warnings)


def test_walking_shares_transit_limit_and_never_collected_or_corrected():
    walking = route('foot', [leg(mode='walk', duration=5000)])
    planner = Planner([route('bus-a'), walking, route('bus-b')])
    collected = []
    result = TrafficEngine(planner, graph_loader=lambda **kwargs: Graph(),
        collector=lambda candidates, *args: (collected.extend(candidates) or [], []),
        clock=lambda: 0, wall_clock=lambda: NOW).plan(request(max_results=1))
    assert [r.id for r in result.routes] == ['foot']
    assert [r.id for r in collected] == ['bus-a', 'bus-b']
    foot = result.routes[-1]
    assert foot.duration_seconds == 5000
    assert foot.debug.evaluation_group == 'not_applicable'
    assert foot.debug.coverage.eligible_distance_meters == 0
    assert all(l.debug.traffic_status == 'not_applicable' for l in foot.legs)
    assert [r.debug.final_rank for r in result.routes] == [1]
    assert result.debug.candidate_count == 3 and result.debug.returned_count == 1


def test_walking_survives_excluded_broken_transit_and_failed_tail():
    broken = route('broken', [leg(duration=600), leg('second', start=610, duration=300)])
    walking = route('foot', [leg(mode='walk', duration=5000)])
    planner = Planner([broken, walking], tails=TimeoutError('deadline'))
    result = engine(planner, Graph(), [road(seconds=900)]).plan(request(max_results=1))
    assert [r.id for r in result.routes] == ['foot']
    assert result.debug.excluded_count == 1
    assert result.debug.excluded_candidates[0].reason_code == 'boarding_unreachable'
    assert result.routes[0].debug.evaluation_group == 'not_applicable'


def test_all_excluded_transit_queries_real_walking_fallback_within_deadline():
    broken = route('broken', [leg(duration=600), leg('second', start=610, duration=300)])
    walking = route('foot', [leg(mode='walk', duration=5000)])

    class FallbackPlanner(Planner):
        def plan(self, req, **kwargs):
            if req.modes == ['walk']:
                self.calls.append((req.model_copy(deep=True), kwargs))
                return response([walking])
            return super().plan(req, **kwargs)

    planner = FallbackPlanner([broken], tails=TimeoutError('tail unavailable'))
    result = engine(planner, Graph(), [road(seconds=900)]).plan(request(max_results=1))
    assert [r.id for r in result.routes] == ['foot']
    assert planner.calls[-1][0].modes == ['walk']
    assert result.debug.candidate_count == 2
    assert result.debug.returned_count == 1 and result.debug.excluded_count == 1
    assert result.debug.provider_calls[-1].purpose == 'initial_candidates'
    assert result.routes[0].debug.final_rank == 1
