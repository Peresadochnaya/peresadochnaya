from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from app import schemas as s
from app.traffic_data import SegmentBaseline, RoadObservation
from app.traffic_engine import TrafficEngine
from app.traffic_planner import ObservationCollector, plan_waypoints
from app.traffic_cache import TrafficCache

NOW=1794484800
START=datetime.fromtimestamp(NOW,timezone.utc)
A=(30.,60.); B=(30.01,60.)

def route():
    a=s.Place(lon=A[0],lat=A[1],name='a',stop_id='2:a')
    b=s.Place(lon=B[0],lat=B[1],name='b',stop_id='2:b')
    leg=s.Leg(mode='bus',origin=a,destination=b,start_at=START,end_at=START+timedelta(seconds=60),duration_seconds=60,original_duration_seconds=60,duration_source='schedule',distance_meters=556,geometry=s.LineString(coordinates=[A,B]),line=s.TransitLine(route_id='2:r',trip_id='2:t',short_name='1'))
    return s.Route(id='test',name='bus',start_at=START,end_at=leg.end_at,duration_seconds=60,transfers=0,distance_meters=556,geometry=leg.geometry,legs=[leg],provenance=s.RouteProvenance(candidate_providers=['otp'],traffic_providers=[]))

def request():
    return s.RouteRequest(points=[s.Point(lon=A[0],lat=A[1]),s.Point(lon=B[0],lat=B[1])],departure_at=START,traffic_mode='on',modes=['bus'])

def segment():
    return SegmentBaseline('gtfs','v','t','a','b',1,2,(A,B),60,mode='bus',road_status='shared')

class Planner:
    def plan(self,req,**kwargs):
        return s.RoutesResponse(mode='real',sources=s.RoutesResponseSources(candidate_providers=['otp'],traffic_providers=[],realtime='unavailable'),passenger=req.passenger,routes=[route()],gtfs_version='v',graph_version='g',calculated_at=START,warnings=[])

class Graph:
    def trip_segments(self,*args,**kwargs): return [segment()]
    def service_info(self,*args,**kwargs): return {'service_kind':'scheduled'}


def test_real_correction_uses_original_baseline_and_full_diagnostics():
    observation=RoadObservation('o','yandex',(A,B),556,120,NOW)
    engine=TrafficEngine(Planner(),graph_loader=lambda **kw:Graph(),clock=lambda:0,wall_clock=lambda:NOW,
        collector=lambda *args:([observation],[]))
    response=engine.plan(request(),gtfs_version='v',graph_version='g')
    result=response.routes[0]
    assert result.duration_seconds==120
    assert result.legs[0].original_duration_seconds==60
    assert result.debug.evaluation_group=='sufficient'
    assert result.provenance.traffic_providers==['yandex']
    assert result.legs[0].debug.segments[0].observations[0].selected_for_correction
    assert response.debug.cache_ttl_seconds==1800
    # New searches do not mutate the baseline candidate or compound corrections.
    assert engine.plan(request()).routes[0].duration_seconds==120


def test_missing_active_baseline_is_incomplete_without_paid_calls():
    response=TrafficEngine(Planner(),clock=lambda:0,wall_clock=lambda:NOW).plan(request())
    assert response.routes[0].duration_seconds==60
    assert response.routes[0].debug.evaluation_group=='incomplete'
    assert response.debug.provider_calls[0].billable_units==0


def test_collector_cache_and_paid_budget():
    class Provider:
        def __init__(self): self.calls=0
        def estimate_observations(self,points,timeout_seconds):
            self.calls+=1
            return [RoadObservation('o','2gis',(A,B),556,120,NOW)]
    provider=Provider(); cache=TrafficCache(clock=lambda:NOW)
    collect=ObservationCollector(provider,provider,cache=cache,clock=lambda:0,wall_clock=lambda:NOW)
    observations,calls=collect([route()],Graph(),15)
    assert provider.calls==1
    assert [c.billable_units for c in calls]==[1,0]
    collect([route()],Graph(),15)
    assert provider.calls==1


def test_waypoint_quota_counts_connectors_and_repeated_stops():
    segments=[segment()]
    points,targets,connectors=plan_waypoints(segments,max_points=2)
    assert points==[A,B]
    assert len(targets)==1 and not connectors
    assert plan_waypoints(segments,max_points=1)[0]==[]


def test_closed_loop_keeps_intermediate_anchors():
    from dataclasses import replace
    c=(30.01,60.01)
    base=segment()
    parts=[replace(base,from_stop_id='a',to_stop_id='b',geometry=(A,B)),
        replace(base,from_stop_id='b',to_stop_id='c',geometry=(B,c)),
        replace(base,from_stop_id='c',to_stop_id='a',geometry=(c,A))]
    points,targets,_=plan_waypoints(parts)
    assert len(points)>=3
    assert len(targets)==3
    assert len(set(points))>=3


def test_long_candidate_corridors_share_anchor_quota():
    from dataclasses import replace
    base=segment(); parts=[]
    for branch in range(2):
        for i in range(100):
            a=(30+i*.001,60+branch*.1)
            b=(30+(i+1)*.001,60+branch*.1)
            parts.append(replace(base,from_stop_id=f'{branch}:{i}',to_stop_id=f'{branch}:{i+1}',geometry=(a,b)))
    points,targets,_=plan_waypoints(parts)
    assert len(points)<=50
    assert len(targets)==200
    assert sum(p[1]==60 for p in points)>=3
    assert sum(p[1]==60.1 for p in points)>=3


def test_paid_collection_diagnostic_records_original_ab_and_connectors():
    class Provider:
        def estimate_observations(self,points,timeout_seconds): return []
    provider=Provider()
    collect=ObservationCollector(provider,provider,cache=TrafficCache(clock=lambda:NOW),clock=lambda:0,wall_clock=lambda:NOW)
    req=request().model_copy(update={'points':[s.Point(lon=29.99,lat=60),s.Point(lon=30.02,lat=60)]})
    _,calls=collect([route()],Graph(),15,request=req)
    assert calls[0].waypoint_coordinates==[(29.99,60),(30.02,60)]
    assert calls[1].target_edge_ids==['gtfs:a:b']
    assert calls[1].unrepresented_target_edge_ids==[]
    assert calls[1].actual_chain_duration_seconds==0
    assert calls[1].planned_chain_chord_meters>0


def test_otp_stop_origin_preserves_feed_namespace():
    import json
    from pathlib import Path
    import httpx
    from app.otp import OtpPlanner
    fixture=json.loads((Path(__file__).parents[1]/'app/fixtures/otp/plan-response.json').read_text())
    seen=[]
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200,json=fixture)
    planner=OtpPlanner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    planner.plan(request(),origin_stop_id='2:b',timeout_seconds=1)
    assert seen[0]['variables']['origin']=={'location':{'stopLocation':{'stopLocationId':'2:b'}}}


def test_transfer_slack_is_not_added_twice(tmp_path):
    from app.main import _active_transfer_slack
    import json
    config=tmp_path/'otp/current';config.mkdir(parents=True)
    (config/'router-config.json').write_text(json.dumps({'routingDefaults':{'transferSlack':'PT2M','boardSlack':'PT10S'}}))
    assert _active_transfer_slack(tmp_path)==130


def test_far_away_global_cache_is_not_returned_in_search_debug():
    cache=TrafficCache(clock=lambda:NOW)
    far=((130.,40.),(130.01,40.))
    cache.put(('far',),[RoadObservation('far','yandex',far,1000,100,NOW)])
    near=RoadObservation('near','yandex',(A,B),556,120,NOW)
    cache.put(('near',),[near])
    collector=ObservationCollector(None,None,cache=cache,clock=lambda:0,wall_clock=lambda:NOW)
    observations,_=collector([route()],Graph(),15)
    assert [o.id for o in observations]==['near']


def test_search_admission_is_bounded_and_does_not_queue():
    from app.yandex import ProviderError
    engine=TrafficEngine(Planner())
    assert engine._search_slots.acquire(blocking=False)
    assert engine._search_slots.acquire(blocking=False)
    try:
        with pytest.raises(ProviderError) as failed:
            engine.plan(request())
        assert failed.value.status==503
        assert failed.value.code=='traffic_search_busy'
    finally:
        engine._search_slots.release();engine._search_slots.release()


def test_partial_dedicated_lane_is_excluded_from_engine_coverage_denominator():
    from dataclasses import replace
    from app.traffic_geometry import length
    baseline=segment(); total=length(baseline.geometry)
    baseline=replace(baseline,road_status='unknown',lane_spans=({'start_m':0,'end_m':total/2,'status':'dedicated','source':'https://example.test/lanes','observed_at':START,'version':'1'},))
    class PartialGraph(Graph):
        def trip_segments(self,*args,**kwargs): return [baseline]
    observation=RoadObservation('o','yandex',(A,B),total,120,NOW)
    result=TrafficEngine(Planner(),graph_loader=lambda **kw:PartialGraph(),clock=lambda:0,
        wall_clock=lambda:NOW,collector=lambda *args:([observation],[])).plan(request()).routes[0]
    assert result.debug.coverage.eligible_distance_meters==pytest.approx(total/2)
    assert result.debug.coverage.coverage_ratio==pytest.approx(1)
    assert result.debug.evaluation_group=='sufficient'
    assert result.legs[0].debug.segments[0].lane_spans[0]['version']=='1'
