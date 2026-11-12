"""Replay the recorded OTP/Yandex search without network or a live GTFS feed.

The frozen per-trip correction ledger replaces geometry matching only: it holds
road additions computed during the recorded run. Raw OTP itinerary parsing,
Yandex observation parsing, timing, tail replacement, grouping and diagnostics
run through production code. Geometry matching has its own fixture suite.
"""
import json
from pathlib import Path
from dataclasses import replace
from datetime import timedelta
import socket

import httpx
import pytest

from app.otp import OtpPlanner
from app.schemas import RouteRequest
from app.traffic_data import SegmentBaseline, CorrectionResult
from app.traffic_engine import TrafficEngine, collapse_replanned_duplicates, final_journey_key
from app.traffic_geometry import length
from app.yandex_router import parse_observations

FIXTURES=Path(__file__).parent/'fixtures/traffic-replanned-duplicates'

def load(name): return json.loads((FIXTURES/name).read_text())

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('Offline replay attempted a network call'))


def replay(monkeypatch,capture=None,full_coverage=False):
    request=RouteRequest.model_validate(load('request.json'))
    ledger=load('correction-ledger.json')
    if full_coverage:
        for values in ledger.values(): values['coverage_ratio']=1
    bodies=[load('otp-01-response.json'),load('otp-02-response.json')]
    original_bodies=json.dumps(bodies,sort_keys=True)
    initial=[]; raw_requests=[]
    class Planner(OtpPlanner):
        def plan(self,*args,**kwargs):
            value=super().plan(*args,**kwargs)
            if not initial: initial.extend(r.model_copy(deep=True) for r in value.routes)
            return value
    def handler(req):
        raw_requests.append(json.loads(req.content))
        assert len(raw_requests)<=2
        return httpx.Response(200,json=bodies[len(raw_requests)-1])
    planner=Planner(client=httpx.Client(transport=httpx.MockTransport(handler)))
    now=request.departure_at.timestamp()
    observations=parse_observations(load('yandex-response-raw.json'),request.departure_at)
    assert observations and all(o.provider=='yandex' for o in observations)
    class Graph:
        def service_info(self,*args,**kwargs):
            return {'service_kind':'frequency','frequency_exact_times':0,'wait_model':'otp_model'}
        def trip_segments(self,trip_id,**kwargs):
            leg=next((l for r in initial for l in r.legs if l.line and l.line.trip_id==trip_id),None)
            if leg is None: return []
            return [SegmentBaseline('gtfs','recorded',trip_id,leg.origin.stop_id,leg.destination.stop_id,1,2,
                tuple(leg.geometry.coordinates),leg.original_duration_seconds)]
        def transfer_minimum(self,*args,**kwargs): return {'minimum_seconds':None,'forbidden':False}
    def correction(segment,roads,**kwargs):
        assert roads==observations
        values=ledger.get(segment.trip_id,{'added_seconds':0,'coverage_ratio':0})
        size=length(segment.geometry)
        return CorrectionResult(added_seconds=values['added_seconds'],applicable_m=size,
            covered_m=size*values['coverage_ratio'],fragments=[{'observation_id':observations[0].id,
                'provider':'yandex','added_seconds':values['added_seconds']}])
    monkeypatch.setattr('app.traffic_geometry.correct_segment',correction)
    if capture is not None:
        def collect_before_collapse(routes,originals):
            capture.extend(r.model_copy(deep=True) for r in routes)
            return collapse_replanned_duplicates(routes,originals)
        monkeypatch.setattr('app.traffic_engine.collapse_replanned_duplicates',collect_before_collapse)
    result=TrafficEngine(planner,graph_loader=lambda **kw:Graph(),collector=lambda *a: (observations,[]),
        clock=lambda:0,wall_clock=lambda:now,transfer_slack_loader=lambda:120).plan(request)
    assert json.dumps(bodies,sort_keys=True)==original_bodies
    return result,initial,raw_requests


def test_recorded_recovery_convergence_collapses_only_exact_final_chain(monkeypatch):
    corrected=[]
    result,initial,requests=replay(monkeypatch,capture=corrected)
    assert len(requests)==2
    assert len(result.routes)==2
    assert len({final_journey_key(r) for r in result.routes})==2
    duplicates=[e for e in result.debug.excluded_candidates if e.reason_code=='duplicate_after_replanning']
    assert len(duplicates)==1
    excluded=duplicates[0]
    assert excluded.original_route.id=='otp:23c2af5a17257316'
    assert excluded.duplicate_of_route_id=='otp:ce4a07abf87f3c57'
    assert excluded.tail_replanning_status is None
    original=next(r for r in initial if r.id==excluded.original_route.id)
    assert excluded.original_route.model_dump()==original.model_dump()
    winner=next(r for r in result.routes if r.id==excluded.duplicate_of_route_id)
    assert winner.debug.equivalent_candidate_ids==[excluded.original_route.id]
    assert winner.debug.added_delay_seconds==7
    recovered=next(r for r in corrected if r.id==excluded.original_route.id)
    assert recovered.duration_seconds==3408 < recovered.debug.original_duration_seconds==3543
    assert recovered.debug.added_delay_seconds==7
    assert result.debug.pipeline_status=='partial'  # Actual incomplete coverage remains visible.
    assert all(e.reason_code=='duplicate_after_replanning' for e in result.debug.excluded_candidates)


def test_unchanged_otp_duplicates_are_not_backend_filtered(monkeypatch):
    result,initial,_=replay(monkeypatch)
    candidate=result.routes[0].model_copy(deep=True)
    candidate.debug.tail_replanning_status='not_needed'
    other=candidate.model_copy(deep=True);other.id='raw-otp-other'
    kept,excluded=collapse_replanned_duplicates([candidate,other],initial)
    assert len(kept)==2 and excluded==[]


@pytest.mark.parametrize('difference',['time','trip','geometry'])
def test_meaningful_final_leg_differences_are_retained(monkeypatch,difference):
    result,initial,_=replay(monkeypatch)
    candidate=result.routes[0].model_copy(deep=True)
    candidate.debug.tail_replanning_status='complete'
    other=candidate.model_copy(deep=True);other.id='different'
    leg=next(l for l in other.legs if l.line)
    if difference=='time': leg.start_at+=timedelta(seconds=1)
    elif difference=='trip': leg.line.trip_id+=':different'
    else:
        points=list(leg.geometry.coordinates);points[0]=(points[0][0]+.000001,points[0][1]);leg.geometry.coordinates=points
    kept,excluded=collapse_replanned_duplicates([candidate,other],initial)
    assert len(kept)==2 and excluded==[]


def test_benign_duplicate_does_not_mark_pipeline_partial(monkeypatch):
    result,_,_=replay(monkeypatch,full_coverage=True)
    assert len(result.routes)==2
    assert result.debug.excluded_count==1
    assert result.debug.excluded_candidates[0].reason_code=='duplicate_after_replanning'
    assert result.debug.pipeline_status=='complete'
