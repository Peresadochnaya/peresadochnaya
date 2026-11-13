#!/usr/bin/env python3
"""Replay all three recorded live searches against the real pinned GTFS index.

All OTP HTTP responses and Yandex observations are recordings. Socket connects
are forbidden. No paid-provider client exists; original artifacts stay untouched.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
ROOT = BACKEND.parent / 'research/traffic-correction/bus-lanes'
CASES = ('live-search', 'live-search-pionerskaya-rodeo', 'live-search-moskovskaya-pulkovo')


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-dir', type=Path, default=Path('/tmp/peresadochnaya-gtfs-index'))
    parser.add_argument('--output', type=Path, default=ROOT/'offline-fixed-cache-order')
    args = parser.parse_args()
    import httpx
    from app.otp import OtpPlanner
    from app.schemas import RouteRequest, ProviderCallDebug
    from app.traffic_engine import TrafficEngine
    from app.traffic_graph import TrafficGraph, _PinnedSnapshot
    from app.traffic_planner import candidate_segments
    from app.yandex_router import parse_observations

    def forbidden(*args, **kwargs):
        raise RuntimeError('Offline replay prohibits all network connections')
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    profile_path = BACKEND/'app/fixtures/traffic/road-status.json'
    profile = json.loads(profile_path.read_text())
    digest = profile['feed_sha256']['gtfs']
    index = args.index_dir/f'v1-{digest}.sqlite3'
    summaries = []
    for case in CASES:
        source = ROOT/case
        target = args.output/case
        target.mkdir(parents=True, exist_ok=True)
        original = json.loads((source/'response-corrected-full.json').read_text())
        request = RouteRequest.model_validate_json((source/'request.json').read_text())
        recorded = sorted(source.glob('otp-*-response.json'))
        raw_bodies = [json.loads(p.read_text()) for p in recorded]
        observed = max(datetime.fromisoformat(o['observed_at'].replace('Z','+00:00'))
                       for o in original['debug']['observations'])
        observations = parse_observations(json.loads((source/'yandex-response-raw.json').read_text()), observed)
        wall = observed.timestamp()+.001
        graph = TrafficGraph(_PinnedSnapshot(index,digest), graph_version='a8ec4b10f85dd4ce')
        graph.load_road_statuses(profile_path)
        requests = []
        mismatches = []
        consumed = []
        recorded_requests = [json.loads(p.with_name(p.name.replace('-response.json','-request.json')).read_text()) for p in recorded]
        def handler(req):
            requests.append(json.loads(req.content))
            if len(requests)>len(raw_bodies):
                raise RuntimeError('No recorded OTP response available; network fallback prohibited')
            position = len(requests)-1
            if requests[position] != recorded_requests[position]:
                mismatches.append({'reason':'recorded_tail_input_mismatch', 'request_number':position+1,
                    'recorded_variables':recorded_requests[position]['variables'],
                    'replayed_variables':requests[position]['variables']})
                raise RuntimeError('recorded_tail_input_mismatch')
            consumed.append(position+1)
            return httpx.Response(200,json=raw_bodies[position])
        def collector(routes, loaded_graph, deadline, **kwargs):
            # Preserve production collector's candidate-shape pre-read, which
            # detects incompatible stop-pair variants before selecting evidence.
            candidate_segments(routes, loaded_graph, deadline)
            candidate_segments(routes, loaded_graph, deadline)
            return observations, [ProviderCallDebug(provider='yandex',purpose='traffic_collection',
                status='cache_hit',billable_units=0,
                skip_reason='Offline replay of original recorded realtime observations')]
        started = time.monotonic()
        try:
            with httpx.Client(transport=httpx.MockTransport(handler),trust_env=False) as client:
                engine = TrafficEngine(OtpPlanner(client=client),graph_loader=lambda **kwargs:graph,
                    collector=collector,wall_clock=lambda:wall,transfer_slack_loader=lambda:120)
                response = engine.plan(request,gtfs_version=digest,graph_version='a8ec4b10f85dd4ce')
            dump(target/'response-actual-index.json',response.model_dump(mode='json'))
            summary={'case':case,'status':'succeeded','network_calls':0,'paid_calls':0,
                'elapsed_seconds':time.monotonic()-started,'otp_recordings_consumed':len(consumed), 'otp_requests_attempted':len(requests),
                'recorded_tail_input_mismatch':bool(mismatches),
                'replay_limitation':'A changed tail request has no corresponding recording; no claim about a live OTP failure or actual tail feasibility' if mismatches else None,
                'otp_recordings_available':len(raw_bodies), 'profile_version':profile['version'],
                'profile_sha256':hashlib.sha256(profile_path.read_bytes()).hexdigest(),
                'profile_summary':profile['summary'],'gtfs_source_sha256':digest,
                'observations_received_at':observed.isoformat(),'pipeline_status':response.debug.pipeline_status,
                'returned_routes':len(response.routes),'excluded_count':response.debug.excluded_count,
                'routes':[{'id':r.id,'name':r.name,'duration_seconds':r.duration_seconds,
                    'original_duration_seconds':r.debug.original_duration_seconds,
                    'added_delay_seconds':r.debug.added_delay_seconds,
                    'quality':r.debug.evaluation_group,'coverage_ratio':r.debug.coverage.usable_coverage_ratio,
                    'equivalent_candidate_ids':r.debug.equivalent_candidate_ids,
                    'legs':[{'mode':l.mode,'line':l.line.short_name if l.line else None,
                             'duration_seconds':l.duration_seconds,'original_duration_seconds':l.original_duration_seconds}
                            for l in r.legs]} for r in response.routes]}
        except Exception as error:
            summary={'case':case,'status':'failed','network_calls':0,'paid_calls':0,
                'error_type':type(error).__name__,'error_code':getattr(error,'code',None),
                'elapsed_seconds':time.monotonic()-started,'otp_recordings_consumed':len(requests)}
        dump(target/'summary-actual-index.json',summary)
        dump(target/'otp-requests-replayed.json',requests)
        dump(target/'recorded-input-mismatches.json',mismatches)
        summaries.append(summary)
        print(json.dumps(summary,ensure_ascii=False))
    dump(args.output/'summary-actual-index-all.json',summaries)
    return int(any(s['status']!='succeeded' for s in summaries))


if __name__=='__main__':
    raise SystemExit(main())
