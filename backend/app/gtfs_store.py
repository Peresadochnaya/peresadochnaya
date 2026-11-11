"""Read-only, bounded-memory access to immutable ZIP snapshots.

A request opens the ZIP before reading tables, so unlinking an old version during
publication cannot change the snapshot halfway through a response. Large tables
are streamed; only stops/routes/trip metadata and compact associations are cached.
"""
import csv
import hashlib
import io
import json
import re
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from .gtfs_schemas import (GtfsDeparture, GtfsRoute, GtfsStop, GtfsStopTime,
                           GtfsTrip, GtfsTripSummary)

DEMO_GTFS_PATH = Path(__file__).parent / 'fixtures' / 'gtfs' / 'demo.zip'
DEMO_WARNING = 'Demonstration subset of real SPb GTFS; synthetic all-week calendar, not live service availability'
TIME_RE = re.compile(r'^\d{2,}:[0-5]\d:[0-5]\d$')


class GtfsError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def optional(value):
    return value if value not in ('', None) else None


def seconds(value):
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ValueError('Invalid GTFS time')
    h, m, s = map(int, value.split(':'))
    return h * 3600 + m * 60 + s


def service_instant(day, offset, tz):
    # GTFS time is measured from local noon minus 12 elapsed hours, which is
    # important on a DST transition day; adding to naive midnight is incorrect.
    noon = datetime.combine(day, time(12), tzinfo=tz)
    return (noon.astimezone(timezone.utc) - timedelta(hours=12) + timedelta(seconds=offset)).astimezone(tz)


def route_mode(value):
    n = int(value)
    if n in (0, 900, 901, 902, 903, 904, 905, 906): return 'tram'
    if n in (1, 400, 401, 402, 403): return 'metro'
    if n in (2,) or 100 <= n <= 199: return 'rail'
    if n in (3,) or 700 <= n <= 799: return 'bus'
    if n == 11 or 800 <= n <= 899: return 'trolleybus'
    return 'unknown'


@dataclass
class Index:
    stops: dict
    routes: dict
    trips: dict
    timezones: dict
    stop_routes: dict | None = None


class GtfsStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache = {}  # One current metadata index per real/demo mode.
        self._lock = threading.RLock()
        self._requests = threading.BoundedSemaphore(2)

    @contextmanager
    def open(self, dummy=False):
        # Bound concurrent streaming-query working sets under the API pod's
        # 512MiB limit; never retain full stop_times/shapes tables.
        with self._requests:
            with self._open_snapshot(dummy) as snapshot:
                yield snapshot

    @contextmanager
    def _open_snapshot(self, dummy=False):
        stream = None
        try:
            if dummy:
                stream = DEMO_GTFS_PATH.open('rb')
                key = hashlib.sha256(DEMO_GTFS_PATH.read_bytes()).hexdigest()
                version = None
            else:
                # Retry only local publication races, never remote/API requests.
                for attempt in range(3):
                    pointer = json.loads((self.root / 'gtfs' / 'current.json').read_text())
                    version = pointer['sha256']
                    if not isinstance(version, str) or not re.fullmatch(r'[a-f0-9]{64}', version):
                        raise ValueError('Invalid version pointer')
                    try:
                        stream = (self.root / 'gtfs' / 'versions' / version / 'feed.zip').open('rb')
                        break
                    except FileNotFoundError:
                        if attempt == 2: raise
                key = version
            with stream, zipfile.ZipFile(stream) as archive:
                snapshot = Snapshot(self, archive, key, version, dummy)
                yield snapshot
        except GtfsError:
            raise
        except (OSError, ValueError, KeyError, TypeError, csv.Error, UnicodeError,
                zipfile.BadZipFile, ValidationError, ZoneInfoNotFoundError, OverflowError):
            raise GtfsError(503, 'gtfs_unavailable', 'GTFS snapshot is missing or invalid') from None
        finally:
            if stream is not None and not stream.closed: stream.close()


class Snapshot:
    def __init__(self, store, archive, key, version, dummy):
        self.store, self.archive, self.key = store, archive, key
        self.version, self.dummy = version, dummy

    @property
    def metadata(self):
        return dict(mode='dummy' if self.dummy else 'gtfs', gtfs_version=self.version,
                    warnings=[DEMO_WARNING] if self.dummy else [])

    def rows(self, name, required=True):
        try:
            member = self.archive.getinfo(name)
        except KeyError:
            if required: raise ValueError('Missing GTFS table')
            return
        # Never extract archive paths; known root-level names only.
        if member.file_size > 1024 * 1024 * 1024:
            raise ValueError('GTFS table is too large')
        with self.archive.open(member) as raw:
            with io.TextIOWrapper(raw, encoding='utf-8-sig') as text:
                yield from csv.DictReader(text)

    @property
    def index(self):
        with self.store._lock:
            cached = self.store._cache.get(self.dummy)
            if cached is not None and cached[0] == self.key: return cached[1]
            stops = {}
            for r in self.rows('stops.txt'):
                s = GtfsStop(stop_id=r['stop_id'], name=r['stop_name'], lat=float(r['stop_lat']),
                    lon=float(r['stop_lon']), code=optional(r.get('stop_code')),
                    description=optional(r.get('stop_desc')), parent_station=optional(r.get('parent_station')),
                    zone_id=optional(r.get('zone_id')), location_type=int(r.get('location_type') or 0))
                if s.stop_id in stops: raise ValueError('Duplicate stop')
                stops[s.stop_id] = s
            routes = {}
            for r in self.rows('routes.txt'):
                line = GtfsRoute(route_id=r['route_id'], agency_id=optional(r.get('agency_id')),
                    short_name=r.get('route_short_name') or '', long_name=r.get('route_long_name') or '',
                    description=optional(r.get('route_desc')), mode=route_mode(r['route_type']),
                    route_type=int(r['route_type']), color=optional(r.get('route_color')),
                    text_color=optional(r.get('route_text_color')))
                if line.route_id in routes: raise ValueError('Duplicate route')
                routes[line.route_id] = line
            trips = {}
            for r in self.rows('trips.txt'):
                t = GtfsTripSummary(trip_id=r['trip_id'], route_id=r['route_id'], service_id=r['service_id'],
                    headsign=optional(r.get('trip_headsign')), direction_id=int(r['direction_id']) if r.get('direction_id') else None,
                    shape_id=optional(r.get('shape_id')))
                if t.route_id not in routes or t.trip_id in trips: raise ValueError('Invalid trip reference')
                trips[t.trip_id] = t
            zones = {}
            agency_rows = list(self.rows('agency.txt'))
            for r in agency_rows: zones[r.get('agency_id') or ''] = ZoneInfo(r['agency_timezone'])
            if not zones: raise ValueError('Missing agency timezone')
            if len(zones) == 1: zones[None] = next(iter(zones.values()))
            index = Index(stops, routes, trips, zones)
            self.store._cache[self.dummy] = self.key, index
            return index

    @property
    def stops(self): return self.index.stops
    @property
    def routes(self): return self.index.routes
    @property
    def trips(self): return self.index.trips

    def get_stop(self, stop_id):
        try: return self.stops[stop_id]
        except KeyError: raise GtfsError(404, 'stop_not_found', 'Stop was not found in this GTFS snapshot') from None

    def get_route(self, route_id):
        try: return self.routes[route_id]
        except KeyError: raise GtfsError(404, 'transit_route_not_found', 'Transit line was not found in this GTFS snapshot') from None

    def get_trip(self, trip_id):
        try: return self.trips[trip_id]
        except KeyError: raise GtfsError(404, 'trip_not_found', 'Trip was not found in this GTFS snapshot') from None

    def stop_routes(self):
        with self.store._lock:
            if self.index.stop_routes is None:
                result = {}
                for row in self.rows('stop_times.txt'):
                    trip = self.trips[row['trip_id']]
                    if row['stop_id'] not in self.stops: raise ValueError('Unknown stop')
                    result.setdefault(row['stop_id'], set()).add(trip.route_id)
                self.index.stop_routes = result
            return self.index.stop_routes

    def trip(self, trip_id):
        summary = self.get_trip(trip_id)
        times = []
        for row in self.rows('stop_times.txt'):
            if row['trip_id'] != trip_id: continue
            for field in ('arrival_time', 'departure_time'):
                if row.get(field): seconds(row[field])
            times.append(GtfsStopTime(stop=self.stops[row['stop_id']], stop_sequence=int(row['stop_sequence']),
                arrival_time=optional(row.get('arrival_time')), departure_time=optional(row.get('departure_time')),
                pickup_type=int(row['pickup_type']) if row.get('pickup_type') else None,
                drop_off_type=int(row['drop_off_type']) if row.get('drop_off_type') else None,
                timepoint=int(row['timepoint']) if row.get('timepoint') else None))
        times.sort(key=lambda t:t.stop_sequence)
        if len({t.stop_sequence for t in times}) != len(times): raise ValueError('Duplicate stop sequence')
        shape = []
        if summary.shape_id:
            for row in self.rows('shapes.txt', required=False):
                if row['shape_id'] == summary.shape_id:
                    shape.append((int(row['shape_pt_sequence']), float(row['shape_pt_lon']), float(row['shape_pt_lat'])))
        shape.sort()
        geometry = {'type':'LineString', 'coordinates':[(r[1],r[2]) for r in shape]} if len(shape) >= 2 else None
        warnings = ['shape_unavailable: GTFS has no usable shape for this trip'] if geometry is None else []
        if not times: warnings.append('stop_times_unavailable: GTFS trip has no stop times')
        return GtfsTrip(**summary.model_dump(), stop_times=times, geometry=geometry), warnings

    def _calendar(self, days):
        active = {d:set() for d in days}
        coverage = set()
        for row in self.rows('calendar.txt', required=False):
            start, end = datetime.strptime(row['start_date'],'%Y%m%d').date(), datetime.strptime(row['end_date'],'%Y%m%d').date()
            if start > end: continue
            for day in days:
                if start <= day <= end:
                    coverage.add(day)
                    if row[('monday','tuesday','wednesday','thursday','friday','saturday','sunday')[day.weekday()]] == '1':
                        active[day].add(row['service_id'])
        for row in self.rows('calendar_dates.txt', required=False):
            day = datetime.strptime(row['date'],'%Y%m%d').date()
            if day not in active: continue
            coverage.add(day)
            if row['exception_type'] == '1': active[day].add(row['service_id'])
            elif row['exception_type'] == '2': active[day].discard(row['service_id'])
            else: raise ValueError('Invalid service exception')
        return active, coverage

    def departures(self, stop_id, start, lookahead_minutes, limit):
        self.get_stop(stop_id)
        try: end = start + timedelta(minutes=lookahead_minutes)
        except OverflowError: raise GtfsError(422, 'invalid_departure_time', 'Departure window exceeds supported datetime range') from None
        selected, first = [], {}
        # Compact first-stop metadata is needed for frequency offsets. Do not
        # retain the entire 190MB stop_times table in memory.
        for row in self.rows('stop_times.txt'):
            trip_id = row['trip_id']
            sequence = int(row['stop_sequence'])
            if trip_id not in first or sequence < first[trip_id][0]: first[trip_id] = sequence, row.get('departure_time')
            if row['stop_id'] == stop_id and row.get('pickup_type') != '1' and row.get('departure_time'):
                selected.append(row)
        frequencies = {}
        wanted = {r['trip_id'] for r in selected}
        for row in self.rows('frequencies.txt', required=False):
            if row['trip_id'] in wanted: frequencies.setdefault(row['trip_id'],[]).append(row)
        events, days = [], set()
        for row in selected:
            trip = self.trips[row['trip_id']]
            line = self.routes[trip.route_id]
            tz = self.index.timezones.get(line.agency_id)
            if tz is None: raise ValueError('Unknown agency timezone')
            days.update((start.astimezone(tz).date(), end.astimezone(tz).date()))
            offset = seconds(row['departure_time'])
            intervals = frequencies.get(trip.trip_id)
            if intervals:
                base = seconds(first[trip.trip_id][1])
                for f in intervals:
                    begin, finish = seconds(f['start_time']), seconds(f['end_time'])
                    headway = int(f['headway_secs'])
                    exact = int(f.get('exact_times') or 0)
                    if begin >= finish or headway <= 0 or exact not in (0,1): raise ValueError('Invalid frequency')
                    if offset < base: raise ValueError('Negative stop offset')
                    events.append((row,trip,line,tz,begin+offset-base,finish+offset-base,headway,exact))
            else: events.append((row,trip,line,tz,offset,None,None,None))
        if not days:
            days.update(start.astimezone(tz).date() for tz in self.index.timezones.values())
        candidate_events = []
        for event in events:
            row,trip,line,tz,begin,finish,headway,exact = event
            lower = start.astimezone(tz).date() - timedelta(days=(finish if finish is not None else begin)//86400+1)
            upper = end.astimezone(tz).date() - timedelta(days=begin//86400-1)
            if (upper-lower).days > 370: raise ValueError('Unreasonable frequency interval')
            day = lower
            while day <= upper:
                at = service_instant(day,begin,tz)
                until = service_instant(day,finish,tz) if finish is not None else None
                if (until is None and start <= at < end) or (until is not None and at < end and until > start):
                    candidate_events.append((event,day,at,until));days.add(day)
                day += timedelta(days=1)
        active, coverage = self._calendar(days)
        if not (coverage & days):
            raise GtfsError(422, 'invalid_service_date', 'GTFS calendar does not cover the requested service date')
        results, interval_present = [], False
        def sort_key(item):
            at=item.scheduled_departure_at or max(start,item.frequency_start_at)
            return at.astimezone(timezone.utc), item.trip_id, item.stop_sequence
        def retain(item):
            # Only the first limit+1 entries are needed for pagination. A
            # one-second headway over a day must not allocate 86k models/trip.
            results.append(item)
            results.sort(key=sort_key)
            if len(results)>limit+1: results.pop()
        for event,day,at,until in candidate_events:
            row,trip,line,tz,begin,finish,headway,exact = event
            if trip.service_id not in active[day]: continue
            common = dict(trip_id=trip.trip_id, service_id=trip.service_id, stop_id=stop_id,
                stop_sequence=int(row['stop_sequence']), line=line, headsign=trip.headsign, service_date=day)
            if exact == 0:
                interval_present = True
                retain(GtfsDeparture(**common, scheduled_departure_at=None, departure_time=None,
                    schedule_type='frequency', headway_seconds=headway, frequency_start_at=at, frequency_end_at=until))
            elif exact == 1:
                # Generate only grid events inside the requested window, not
                # every departure since service-day start.
                elapsed = (start.astimezone(timezone.utc)-at.astimezone(timezone.utc)).total_seconds()
                import math
                n = max(0,math.ceil(elapsed/headway))
                emitted=0
                while emitted < limit+1:
                    current = at.astimezone(timezone.utc)+timedelta(seconds=n*headway)
                    if current >= min(end.astimezone(timezone.utc),until.astimezone(timezone.utc)): break
                    raw = begin+n*headway
                    h,rem=divmod(raw,3600);m,s=divmod(rem,60)
                    retain(GtfsDeparture(**common, scheduled_departure_at=current.astimezone(tz),
                        departure_time=f'{h:02d}:{m:02d}:{s:02d}', headway_seconds=headway))
                    n+=1
                    emitted+=1
            else:
                retain(GtfsDeparture(**common, scheduled_departure_at=at, departure_time=row['departure_time']))
        results.sort(key=sort_key)
        warnings=['frequency_based_departures: interval only, not a precise departure'] if interval_present else []
        return results[:limit],len(results)>limit,warnings


def demo_stops():
    with GtfsStore(Path('/unused')).open(dummy=True) as snapshot:
        return [s.model_dump(include={'stop_id','name','lat','lon'}) for _,s in sorted(snapshot.stops.items())]
