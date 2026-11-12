"""One bounded generation: download observations, run C++, write a candidate feed.

The parent publishes the candidate only after validation. This process never
writes the current feed. Static trip IDs always come from OTP's loaded graph.
"""
import argparse
from contextlib import closing
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2 as pb

from .trip_update_builder import build_trip_update
from .trip_update_serializer import serialize_feed

URL = 'https://transport.orgp.spb.ru/Portal/transport/internalapi/gtfs/realtime/vehicle'
MOSCOW = ZoneInfo('Europe/Moscow')
MAX_BYTES = 20 * 1024 * 1024


def seconds(value):
    h, m, s = map(int, value.split(':'))
    if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
        raise ValueError('Invalid GTFS time')
    return h * 3600 + m * 60 + s


def static_database(root):
    metadata = json.loads((root / 'otp/current/manifest.json').read_text())
    digest = metadata['gtfs_source_sha256']
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid graph source digest')
    path = root / 'gtfs-index' / f'v1-{digest}.sqlite3'
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA cache_size=-16384')
    return db


def download():
    import ssl
    context = ssl.create_default_context(cafile=os.getenv('GTFS_RT_CA_FILE', '/service/certs/ca.pem'))
    payload = bytearray()
    with httpx.Client(verify=context, timeout=20, follow_redirects=True) as client:
        with client.stream('GET', os.getenv('GTFS_RT_SOURCE_URL', URL)) as response:
            response.raise_for_status()
            for block in response.iter_bytes():
                payload.extend(block)
                if len(payload) > MAX_BYTES:
                    raise ValueError('Upstream feed exceeds size limit')
    feed = pb.FeedMessage.FromString(payload)
    if not feed.IsInitialized() or feed.header.incrementality != pb.FeedHeader.FULL_DATASET:
        raise ValueError('Expected complete GTFS-RT snapshot')
    age = time.time() - feed.header.timestamp
    if not feed.header.HasField('timestamp') or not -30 <= age <= 90:
        raise ValueError('Upstream feed is stale or has no timestamp')
    return feed


def route_mode(value):
    n = int(value)
    if n == 0 or 900 <= n <= 906: return 'tram'
    if n == 11 or 800 <= n <= 899: return 'trolleybus'
    if n == 3 or 700 <= n <= 799: return 'bus'
    if n == 1 or 400 <= n <= 403: return 'metro'
    if n == 2 or 100 <= n <= 199: return 'rail'
    return 'unknown'


def prepare(db, vehicle):
    descriptor = vehicle.trip
    if not descriptor.trip_id:
        return None
    trip = db.execute('SELECT * FROM gtfs_trips WHERE trip_id=?', (descriptor.trip_id,)).fetchone()
    if trip is None or (descriptor.route_id and descriptor.route_id != trip['route_id']):
        return None
    if descriptor.HasField('direction_id') and trip['direction_id'] not in ('', None, str(descriptor.direction_id)):
        return None
    stops = [dict(r) for r in db.execute('''SELECT st.*,s.stop_lat,s.stop_lon FROM gtfs_stop_times st
        JOIN gtfs_stops s USING(stop_id) WHERE st.trip_id=? ORDER BY CAST(st.stop_sequence AS INTEGER)''', (descriptor.trip_id,))]
    if len(stops) < 2 or any(not s['arrival_time'] or not s['departure_time'] for s in stops):
        return None
    # Frequency trips without an actual instance start cannot be identified.
    has_frequency = db.execute("SELECT 1 FROM catalogue WHERE name='frequencies.txt'").fetchone()
    frequencies = list(db.execute('SELECT * FROM gtfs_frequencies WHERE trip_id=?', (descriptor.trip_id,))) if has_frequency else []
    if frequencies and (not descriptor.start_time or not descriptor.start_date):
        return None
    if descriptor.start_date:
        day = datetime.strptime(descriptor.start_date, '%Y%m%d').date()
    else:
        # Infer service day with >24h times. A reported current stop makes the
        # choice explicit; otherwise match the whole trip's operating interval.
        local = datetime.fromtimestamp(vehicle.timestamp, MOSCOW)
        target = next((s for s in stops if vehicle.HasField('current_stop_sequence') and int(s['stop_sequence']) == vehicle.current_stop_sequence), None)
        low = seconds((target or stops[0])['arrival_time'])
        high = seconds((target or stops[-1])['departure_time'])
        def gap(d):
            midnight = datetime.combine(d, datetime.min.time(), MOSCOW).timestamp()
            return max(midnight+low-vehicle.timestamp, vehicle.timestamp-midnight-high, 0)
        day = min((local.date()+timedelta(days=i) for i in (-2,-1,0,1)), key=gap)
        if gap(day) > 7200: return None
    base = datetime.combine(day, datetime.min.time(), MOSCOW).timestamp()
    offset = seconds(descriptor.start_time)-seconds(stops[0]['departure_time']) if frequencies else 0
    unscheduled = bool(frequencies) and any(f['exact_times'] in ('', None, '0') for f in frequencies)
    for s in stops:
        s['arrival'] = int(base + seconds(s['arrival_time']) + offset)
        s['departure'] = int(base + seconds(s['departure_time']) + offset)
    if any(stops[i]['departure'] > stops[i+1]['arrival'] for i in range(len(stops)-1)):
        return None
    shape = [(float(r['shape_pt_lat']), float(r['shape_pt_lon'])) for r in db.execute(
        'SELECT * FROM gtfs_shapes WHERE shape_id=? ORDER BY CAST(shape_pt_sequence AS INTEGER)', (trip['shape_id'],))] if trip['shape_id'] else []
    if len(shape)<2: return None
    return {'vehicle': vehicle, 'trip': dict(trip), 'stops': stops, 'shape': shape, 'day': day, 'unscheduled': unscheduled}


def calculate(jobs):
    lines = [str(len(jobs))]
    for job in jobs:
        v, shape, stops = job['vehicle'], job['shape'], job['stops']
        lines.append(f'{v.position.latitude} {v.position.longitude} {v.timestamp} {len(shape)} {len(stops)}')
        lines.extend(f'{lat} {lon}' for lat,lon in shape)
        lines.extend(f"{s['stop_sequence']} {s['arrival']} {s['departure']} {s['stop_lat']} {s['stop_lon']}" for s in stops)
    result = subprocess.run([os.getenv('DELAY_ENGINE_BINARY', '/usr/local/bin/delay-engine')],
        input='\n'.join(lines)+'\n', text=True, capture_output=True, timeout=45, check=True)
    rows = [line.split() for line in result.stdout.splitlines()]
    if len(rows) != len(jobs): raise ValueError('Incomplete C++ response')
    return [(int(seq), int(delay), float(distance)) for seq,delay,distance in rows]


def generate(source, db, *, now=None):
    now = time.time() if now is None else now
    positions, jobs, ids = [], [], set()
    routes = {r['route_id']: route_mode(r['route_type']) for r in db.execute('SELECT route_id,route_type FROM gtfs_routes')}
    for entity in source.entity:
        if not entity.HasField('vehicle') or entity.is_deleted: continue
        v = entity.vehicle
        key = v.vehicle.id or entity.id
        if key in ids or not v.HasField('position') or not v.HasField('timestamp'): continue
        p = v.position
        if not (-90<=p.latitude<=90 and -180<=p.longitude<=180) or not -30 <= now-v.timestamp <=90: continue
        if p.HasField('bearing') and (not math.isfinite(p.bearing) or not 0<=p.bearing<=360): p.ClearField('bearing')
        if p.HasField('speed') and (not math.isfinite(p.speed) or p.speed<0): p.ClearField('speed')
        ids.add(key)
        positions.append((key, v))
        try:
            job = prepare(db, v)
            if job: jobs.append(job)
        except (ValueError, KeyError, TypeError):
            continue
    updates, seen = [], set()
    for job, (seq, delay, distance) in zip(jobs, calculate(jobs) if jobs else []):
        if seq < 0: continue
        v, trip, stops = job['vehicle'], job['trip'], job['stops']
        identity=(v.trip.trip_id, job['day'], v.trip.start_time)
        if identity in seen: continue
        seen.add(identity)
        model = build_trip_update(trip_id=v.trip.trip_id, route_id=trip['route_id'],
            direction_id=int(trip['direction_id']) if trip['direction_id'] in ('0','1') else None,
            start_time=v.trip.start_time or stops[0]['departure_time'], start_date=job['day'],
            vehicle_id=v.vehicle.id, vehicle_label=v.vehicle.label or None,
            vehicle_timestamp=datetime.fromtimestamp(v.timestamp,MOSCOW), current_stop_sequence=seq,
            engine_delays=[{'stop_id':s['stop_id'],'stop_sequence':int(s['stop_sequence']),
                           'arrival_delay':delay,'departure_delay':delay} for s in stops])
        if job['unscheduled']:
            model.trip.schedule_relationship='UNSCHEDULED'
            # frequency exact_times=0 has no fixed schedule: publish absolute
            # predicted times, never misleading delay offsets.
            by_seq={int(s['stop_sequence']):s for s in stops}
            for update in model.stop_time_update:
                for name in ('arrival','departure'):
                    event=getattr(update,name)
                    if event is not None:
                        event.time=datetime.fromtimestamp(by_seq[update.stop_sequence][name]+delay,MOSCOW)
                        event.delay=None
        updates.append(model)
    feed = pb.FeedMessage.FromString(serialize_feed(updates,feed_timestamp=datetime.fromtimestamp(source.header.timestamp,MOSCOW)))
    for key,v in positions:
        e=feed.entity.add(); e.id='vehicle:'+key; e.vehicle.CopyFrom(v)
    return feed, {'vehicles':len(positions),'trip_updates':len(updates),'matched_trips':len(jobs),
                  'route_modes':routes}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,required=True); args=parser.parse_args()
    root=Path(os.getenv('DATA_ROOT','/data'))
    source=download()
    with closing(static_database(root)) as db:
        feed,stats=generate(source,db)
    args.output.write_bytes(feed.SerializeToString())
    # Mode metadata is an optional companion; coordinates and updates are always
    # read from the same atomic protobuf file, never a second positions snapshot.
    args.output.with_suffix('.json').write_text(json.dumps(stats))
    print(json.dumps({k:v for k,v in stats.items() if k!='route_modes'}),flush=True)

if __name__=='__main__': main()
