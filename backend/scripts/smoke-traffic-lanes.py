#!/usr/bin/env python3
"""Prepare (default) or explicitly execute exactly one production traffic search.

No API credential is fetched locally. A persistent exclusive ledger reserves the
only allowed paid Yandex attempt before SSH/network execution; failed attempts
are never retried. Run from any directory with backend/.venv/bin/python.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
DEFAULT_OUTPUT = REPO / 'research/traffic-correction/bus-lanes/live-search'
SENTINEL = 'LOCAL_TRANSPORT_SENTINEL_NOT_A_REAL_KEY'

# Only this remote process reads the real key. Never print request URL, headers,
# exception messages or the credential. Response text is redacted defensively.
REMOTE_HELPER = r'''
import json, os, sys, urllib.request, urllib.parse, urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener=urllib.request.build_opener(NoRedirect)
payload=json.load(sys.stdin)
key=os.environ.get('YANDEX_ROUTER_API_KEY','').strip()
if not key:
    print(json.dumps({'status':503,'body':'{"error":"remote_key_unavailable"}'}));sys.exit(0)
params=payload['params'];params['apikey']=key
url='https://api.routing.yandex.net/v2/route?'+urllib.parse.urlencode(params)
try:
    with opener.open(urllib.request.Request(url),timeout=payload['timeout']) as response:
        status=response.status;body=response.read().decode('utf-8','replace')
except urllib.error.HTTPError as error:
    status=error.code;body=error.read().decode('utf-8','replace')
except Exception:
    status=504;body='{"error":"remote_provider_transport_failure"}'
body=body.replace(key,'[REDACTED]')
print(json.dumps({'status':status,'body':body}))
'''


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')


def coordinate(value):
    import math
    try:
        lat, lon = map(float, value.split(','))
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError
        return lat, lon
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('Expected valid LAT,LON coordinates') from None


def reserve_global_attempt(ledger, case_name):
    """Count the already executed original case and reserve at most three total.

    The shared lock also covers simultaneous different-case executions. A failed
    attempt consumes its reservation permanently, even before a case ledger can
    be written. This function is invoked only at the paid transport boundary.
    """
    import fcntl
    budget_path = DEFAULT_OUTPUT.parent / 'live-search-paid-budget.json'
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    with budget_path.open('a+') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        state = json.loads(content) if content else {'attempt_limit':3, 'reservations':[]}
        reservations = state['reservations']
        recorded = {r['ledger'] for r in reservations}
        # Import pre-guard attempts, including the first authorized live search.
        for existing in sorted(DEFAULT_OUTPUT.parent.glob('live-search*/paid-call-ledger.json')):
            name = str(existing.resolve())
            if name not in recorded:
                reservations.append({'ledger':name, 'provenance':'existing_paid_attempt'})
                recorded.add(name)
        name = str(ledger.resolve())
        if name in recorded or len(reservations) >= 3:
            raise RuntimeError('Paid attempt already reserved or global three-search budget exhausted')
        reservations.append({'ledger':name, 'case_name':case_name,
                             'reserved_at':datetime.now(ZoneInfo('Europe/Moscow')).isoformat()})
        stream.seek(0)
        stream.truncate()
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Perform the one authorized search; consumes at most one Yandex call')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--ledger', type=Path, default=None)
    parser.add_argument('--case-name', default='vosstaniya-smolny')
    parser.add_argument('--origin', type=coordinate, default=(59.931453, 30.358743), metavar='LAT,LON')
    parser.add_argument('--destination', type=coordinate, default=(59.94876, 30.39536), metavar='LAT,LON')
    parser.add_argument('--origin-label', default=None)
    parser.add_argument('--destination-label', default=None)
    parser.add_argument('--ssh-host', default='k3s-master')
    parser.add_argument('--namespace', default='peresadochnaya')
    parser.add_argument('--otp-url', default='http://127.0.0.1:18765')
    parser.add_argument('--graph-version', default='a8ec4b10f85dd4ce')
    parser.add_argument('--index-dir', type=Path, default=Path('/tmp/peresadochnaya-gtfs-index'))
    args = parser.parse_args()
    args.ledger = args.ledger or args.output / 'paid-call-ledger.json'
    profile_path = BACKEND / 'app/fixtures/traffic/road-status.json'
    profile = json.loads(profile_path.read_text())
    digest = profile['feed_sha256']['gtfs']
    preparation = {'execute': args.execute, 'otp_url': args.otp_url,
        'graph_version': args.graph_version, 'gtfs_source_sha256': digest,
        'lane_version': profile['version'], 'paid_call_limit': 1,
        'transfer_slack_seconds':120,
        'transfer_slack_provenance':'Root verified active OTP router config transferSlack=PT2M before this run',
        'paid_provider': 'yandex', '2gis': 'disabled',
        'case_name':args.case_name,
        'origin_label':args.origin_label, 'destination_label':args.destination_label,
        'origin': {'lat':args.origin[0],'lon':args.origin[1]},
        'destination': {'lat':args.destination[0],'lon':args.destination[1]},
        'global_paid_attempt_limit':3,
        'modes':['bus'], 'max_results':3, 'traffic_mode':'on',
        'departure_at':'current Europe/Moscow after graph warmup',
        'ledger':str(args.ledger), 'output':str(args.output)}
    if not args.execute:
        print(json.dumps(preparation, ensure_ascii=False, indent=2))
        return 0
    if args.ledger.exists():
        raise SystemExit('Refusing execution: paid-call ledger already exists; no retry is permitted')
    import httpx
    from app.otp import OtpPlanner
    from app.schemas import RouteRequest
    from app.traffic_cache import TrafficCache
    from app.traffic_engine import TrafficEngine
    from app.traffic_graph import TrafficGraph, _PinnedSnapshot
    from app.traffic_planner import ObservationCollector
    from app.yandex_router import YandexRouter

    os.environ['YANDEX_ROUTER_API_KEY'] = SENTINEL
    os.environ['GTFS_INDEX_DIR'] = str(args.index_dir)
    index_path = args.index_dir / f'v1-{digest}.sqlite3'
    if not index_path.is_file():
        raise SystemExit('Pinned GTFS index is missing; no search executed')
    graph = TrafficGraph(_PinnedSnapshot(index_path, digest), graph_version=args.graph_version)
    graph.load_road_statuses(profile_path)
    graph.warm_topology()  # Explicit startup work outside the 15-second search.

    args.output.mkdir(parents=True, exist_ok=True)
    # A separate exclusive search marker also prohibits concurrent engine runs
    # before either reaches the paid provider boundary.
    search_marker = args.output / 'search-started.json'
    with search_marker.open('x') as stream:
        json.dump({'started_at': datetime.now(ZoneInfo('Europe/Moscow')).isoformat()}, stream)
    write_json(args.output / 'preparation.json', preparation)

    class RemoteYandexTransport(httpx.BaseTransport):
        def handle_request(self, request):
            if request.method != 'GET' or request.url.host != 'api.routing.yandex.net' or request.url.path != '/v2/route':
                raise RuntimeError('Unexpected provider request refused')
            params = dict(request.url.params)
            if params.pop('apikey', None) != SENTINEL:
                raise RuntimeError('Only the sentinel local credential is permitted')
            if set(params) != {'waypoints', 'mode'} or params['mode'] != 'driving':
                raise RuntimeError('Only realtime ordered driving request is permitted')
            count = len(params['waypoints'].split('|'))
            if not 2 <= count <= 50:
                raise RuntimeError('Waypoint quota violated')
            timeout = min(8.0, float(request.extensions.get('timeout', {}).get('read') or 8.0))
            public = {'provider':'yandex', 'method':'GET',
                      'url':'https://api.routing.yandex.net/v2/route', 'params':params,
                      'point_count':count, 'timeout_seconds':timeout}
            reserve_global_attempt(args.ledger, args.case_name)
            args.ledger.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation is the irreversible quota reservation. Even SSH
            # failure leaves this ledger in place, so there is never a retry.
            with args.ledger.open('x') as stream:
                json.dump({'reserved_at':datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),
                           'attempt_limit':1, 'request':public}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            write_json(args.output / 'yandex-request-public.json', public)
            remote = shlex.join(['kubectl', '-n', args.namespace, 'exec', '-i',
                                 'deploy/api', '-c', 'api', '--', 'python', '-c', REMOTE_HELPER])
            try:
                completed = subprocess.run(['ssh', '-F', '/home/ildar/.ssh/config', '-o', 'BatchMode=yes',
                    '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=10',
                    '-o', 'ServerAliveCountMax=3', args.ssh_host, remote],
                    input=json.dumps({'params':params,'timeout':max(.1,timeout-.5)}),
                    text=True, capture_output=True, timeout=timeout, check=False)
                # stderr deliberately never persisted: remote process errors may
                # contain implementation details. Public failure stays generic.
                if completed.returncode != 0:
                    raise RuntimeError('remote_exit')
                result = json.loads(completed.stdout)
                if not isinstance(result.get('status'), int) or not isinstance(result.get('body'), str):
                    raise RuntimeError('remote_schema')
            except Exception:
                write_json(args.output / 'yandex-transport-error.json', {'error':'remote_transport_failure','retry':False})
                raise httpx.TransportError('Remote provider transport failed; attempt consumed') from None
            (args.output / 'yandex-response-raw.json').write_text(result['body'])
            write_json(args.output / 'yandex-response-status.json', {'status':result['status']})
            return httpx.Response(result['status'], content=result['body'].encode(), request=request)

    otp_count = 0
    def record_otp(response):
        nonlocal otp_count
        response.read()
        otp_count += 1
        prefix = args.output / f'otp-{otp_count:02d}'
        prefix.with_name(prefix.name+'-request.json').write_bytes(response.request.content)
        prefix.with_name(prefix.name+'-response.json').write_bytes(response.content)
    started = time.monotonic()
    request = RouteRequest(points=[preparation['origin'], preparation['destination']],
        departure_at=datetime.now(ZoneInfo('Europe/Moscow')), modes=['bus'], max_results=3,
        traffic_mode='on')
    write_json(args.output / 'request.json', request.model_dump(mode='json'))
    try:
        with httpx.Client(transport=RemoteYandexTransport(), trust_env=False) as roads_client, \
             httpx.Client(trust_env=False, event_hooks={'response':[record_otp]}) as otp_client:
            roads = YandexRouter(roads_client)
            collector = ObservationCollector(roads, None, cache=TrafficCache())
            engine = TrafficEngine(OtpPlanner(url=args.otp_url, client=otp_client),
                roads=roads, dgis_roads=None, graph_loader=lambda **kwargs: graph, collector=collector,
                transfer_slack_loader=lambda: 120.0)
            response = engine.plan(request, gtfs_version=digest, graph_version=args.graph_version)
        payload = response.model_dump(mode='json')
        write_json(args.output / 'response-corrected-full.json', payload)
        summary = {'status':'succeeded', 'elapsed_seconds':time.monotonic()-started,
            'otp_http_calls':otp_count, 'paid_attempts':int(args.ledger.exists()),
            '2gis_calls':0, 'route_count':len(response.routes),
            'routes':[{'id':r.id,'duration_seconds':r.duration_seconds,
                       'debug':r.debug.model_dump(mode='json') if r.debug else None} for r in response.routes]}
        write_json(args.output / 'summary.json', summary)
    except Exception as error:
        write_json(args.output / 'summary.json', {'status':'failed',
            'error_type':type(error).__name__, 'error_code':getattr(error,'code',None),
            'elapsed_seconds':time.monotonic()-started, 'otp_http_calls':otp_count,
            'paid_attempts':int(args.ledger.exists()), '2gis_calls':0, 'retry':False})
        print('Search failed; sanitized summary saved; no retry is permitted')
        return 1
    print(f'Search complete; artifacts: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
