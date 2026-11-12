"""Build a dated suburban GTFS snapshot from Yandex Rasp station schedules."""

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from data.gtfs.versioned_static import atomic_json, publish, prune, store_lock

API = 'https://api.rasp.yandex-net.ru/v3.0'
DATA_DIR = Path(__file__).resolve().parent.parent
HUBS = ('s9602494', 's9602496', 's9602497', 's9602498', 's9602499')
MOSCOW = ZoneInfo('Europe/Moscow')
CARRIER_URLS = {'1332': 'https://ppk-piter.ru/'}
FIELDS = {
    'agency.txt': ('agency_id', 'agency_name', 'agency_url', 'agency_timezone'),
    'stops.txt': ('stop_id', 'stop_name', 'stop_lat', 'stop_lon'),
    'routes.txt': ('route_id', 'agency_id', 'route_short_name', 'route_long_name', 'route_type'),
    'trips.txt': ('route_id', 'service_id', 'trip_id', 'trip_headsign'),
    'stop_times.txt': ('trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence'),
    'calendar.txt': ('service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                     'saturday', 'sunday', 'start_date', 'end_date'),
    'calendar_dates.txt': ('service_id', 'date', 'exception_type'),
    'feed_info.txt': ('feed_publisher_name', 'feed_publisher_url', 'feed_lang',
                      'feed_start_date', 'feed_end_date'),
}


class Progress:
    """Print immediate phase changes and a heartbeat during slow API calls."""

    def __init__(self, *, interval=60, clock=time.monotonic, emit=None):
        self.interval = interval
        self.clock = clock
        self.emit = emit or (lambda message: print(message, file=sys.stderr, flush=True))
        self.started = clock()
        self.stage_started = self.started
        self.stage = 'Подготовка'
        self.current = 0
        self.total = None
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.worker = None

    @staticmethod
    def duration(seconds):
        seconds = max(0, round(seconds))
        return f'{seconds // 60:02}:{seconds % 60:02}'

    def report(self):
        with self.lock:
            now = self.clock()
            done = f'{self.current}/{self.total}' if self.total is not None else str(self.current)
            stage_elapsed = now - self.stage_started
            if self.total and self.current:
                remaining = self.duration(stage_elapsed / self.current * (self.total - self.current))
                estimate = f', осталось примерно {remaining}'
            else:
                estimate = ', остаток пока неизвестен'
            message = (f'[{datetime.now(MOSCOW):%H:%M:%S}] {self.stage}: {done}; '
                       f'прошло {self.duration(now - self.started)}{estimate}')
        self.emit(message)

    def set_stage(self, name, *, total=None):
        with self.lock:
            self.stage, self.total, self.current = name, total, 0
            self.stage_started = self.clock()
        self.report()

    def advance(self, amount=1, *, announce=False):
        with self.lock:
            self.current += amount
            complete = self.total is not None and self.current == self.total
        if announce or complete:
            self.report()

    def _heartbeat(self):
        while not self.finished.wait(self.interval):
            self.report()

    def __enter__(self):
        self.worker = threading.Thread(target=self._heartbeat, daemon=True)
        self.worker.start()
        self.report()
        return self

    def __exit__(self, *_):
        self.finished.set()
        self.worker.join(timeout=1)


class RawStore:
    """Persist each successful API response before any GTFS transformation."""

    def __init__(self, directory, *, offline=False, max_age_seconds=3600):
        self.directory = Path(directory)
        self.offline = offline
        self.max_age_seconds = max_age_seconds

    def path(self, endpoint, params):
        request = json.dumps({'endpoint': endpoint, 'params': params}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(request.encode('utf-8')).hexdigest()
        return self.directory / endpoint / f'{digest}.json'

    def read(self, endpoint, params):
        path = self.path(endpoint, params)
        if not path.exists():
            if self.offline:
                raise FileNotFoundError(f'Нет сохранённого ответа {endpoint}: {params}')
            return None
        if (not self.offline and self.max_age_seconds is not None
                and time.time() - path.stat().st_mtime > self.max_age_seconds):
            return None
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('endpoint') != endpoint or saved.get('params') != params:
            raise ValueError(f'Неверный сохранённый ответ: {path}')
        return saved['response']

    def write(self, endpoint, params, response):
        atomic_json(self.path(endpoint, params), {
            'endpoint': endpoint, 'params': params,
            'received_at': datetime.now(MOSCOW).isoformat(), 'response': response,
        })


def api_get(path, params, *, key, get=requests.get, pause=time.sleep, raw_store=None):
    if raw_store is not None:
        cached = raw_store.read(path, params)
        if cached is not None:
            return cached
    pause(0.2)
    response = get(f'{API}/{path}/', params=params, headers={'Authorization': key}, timeout=(10, 60))
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or result.get('error'):
        raise ValueError(f'Invalid Yandex Rasp {path} response')
    if raw_store is not None:
        raw_store.write(path, params, result)
    return result


def collect_schedules(station, day, *, key='test', get=requests.get, pause=time.sleep, raw_store=None):
    """Read both events and every page; refuse a truncated or changing result."""
    result = []
    for event in ('departure', 'arrival'):
        def pages(direction=None):
            offset, total, directions = 0, None, []
            records = []
            while True:
                params = {'station': station, 'date': day.isoformat(),
                          'transport_types': 'suburban', 'event': event, 'offset': offset}
                if direction is not None:
                    params['direction'] = direction
                page = api_get('schedule', params, key=key, get=get, pause=pause, raw_store=raw_store)
                meta = page.get('pagination') or {}
                items = page.get('schedule')
                if (page.get('date') != day.isoformat() or
                        (page.get('station') or {}).get('code') != station or
                        not isinstance(items, list) or not isinstance(meta.get('total'), int) or
                        meta['total'] < 0 or page.get('interval_schedule')):
                    raise ValueError(f'Invalid or unsupported schedule for {station}/{event}')
                if offset == 0:
                    total = meta['total']
                    directions = [row.get('code') for row in page.get('directions', [])]
                if meta.get('offset') != offset:
                    if direction is None and offset:
                        return None, total, directions
                    raise ValueError(f'Incomplete schedule pagination for {station}/{event}')
                if meta['total'] != total or offset + len(items) > total or (not items and offset < total):
                    raise ValueError(f'Incomplete schedule pagination for {station}/{event}')
                records.extend(items)
                offset += len(items)
                if offset == total:
                    return records, total, directions
                if offset > 10000:
                    raise ValueError('Schedule pagination exceeds safety limit')

        records, total, directions = pages()
        if records is None:
            # Some station responses expose pagination metadata but ignore offset.
            # Documented direction filters can recover a complete result.
            codes = [code for code in directions if code not in (None, 'all', 'arrival', 'departure')]
            if not codes:
                raise ValueError(f'Incomplete schedule pagination for {station}/{event}')
            combined = {}
            for code in codes:
                subset, _, _ = pages(code)
                if subset is None:
                    raise ValueError(f'Incomplete schedule pagination for {station}/{event}/{code}')
                for item in subset:
                    identity = (item.get('thread', {}).get('uid'), item.get(event))
                    combined[identity] = item
            if len(combined) != total:
                raise ValueError(f'Direction results do not cover {station}/{event}: {len(combined)}/{total}')
            records = list(combined.values())
        result.extend({'station': station, 'event': event, 'record': item} for item in records)
    return result


def station_coordinates(payload):
    result = {}
    for country in payload['countries']:
        for region in country.get('regions', []):
            for settlement in region.get('settlements', []):
                for station in settlement.get('stations', []):
                    code = (station.get('codes') or {}).get('yandex_code')
                    if code and station.get('latitude') is not None and station.get('longitude') is not None:
                        coords = {'latitude': station['latitude'], 'longitude': station['longitude']}
                        if code in result and result[code] != coords:
                            raise ValueError(f'Conflicting station coordinates: {code}')
                        result[code] = coords
    return result


def parse_time(value):
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        # The thread endpoint omits the offset, while station schedules include
        # +03:00. All observed SPb station events match in Europe/Moscow.
        instant = instant.replace(tzinfo=MOSCOW)
    return instant.astimezone(MOSCOW)


def needs_previous_day_thread(thread, observations):
    """Detect a train arriving just after midnight from the prior service day."""
    differences = []
    for item in observations:
        event = item['event']
        expected = parse_time(item['record'][event])
        times = [parse_time(stop[event]) for stop in thread['stops']
                 if stop['station']['code'] == item['station'] and stop.get(event)]
        if not times:
            return False
        if expected in times:
            differences.append(0)
        elif expected + timedelta(days=1) in times:
            differences.append(1)
        else:
            return False
    return bool(differences) and all(value == 1 for value in differences)


def matches_previous_day_thread(thread, observations, day):
    """Accept a prior-day train only when every observed station event agrees."""
    stops = thread.get('stops')
    if not isinstance(stops, list) or not stops:
        return False
    first = stops[0].get('departure') or stops[0].get('arrival')
    if not first or parse_time(first).date() != day - timedelta(days=1):
        return False
    for item in observations:
        event = item['event']
        value = item['record'].get(event)
        if not value or not any(
                stop.get('station', {}).get('code') == item['station'] and stop.get(event)
                and parse_time(stop[event]) == parse_time(value) for stop in stops):
            return False
    return bool(observations)


def gtfs_time(instant, service_day):
    value = int((instant - datetime.combine(service_day, datetime.min.time(), MOSCOW)).total_seconds())
    if not 0 <= value < 72 * 3600:
        raise ValueError('Thread stop time is outside the service day')
    return f'{value // 3600:02}:{value // 60 % 60:02}:{value % 60:02}'


def write_tables(path, tables):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, fields in FIELDS.items():
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows(tables[name])
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, stream.getvalue().encode('utf-8'))


def build_feed(path, schedules, threads, stations):
    """Convert exact, dated threads to GTFS; never infer missing times/coordinates."""
    if not schedules:
        raise ValueError('No suburban schedules with station coordinates to publish')
    tables = {name: [] for name in FIELDS}
    agency, stops, route_ids, service_days = {}, {}, set(), set()
    observations = {}
    for item in schedules:
        record = item['record']
        if record.get('is_fuzzy') or record.get('thread', {}).get('transport_type') != 'suburban':
            raise ValueError('Fuzzy or non-suburban schedule cannot be published as exact GTFS')
        uid = record['thread']['uid']
        observations.setdefault(uid, []).append(item)
    for uid, seen in sorted(observations.items()):
        thread = threads[uid]
        if thread.get('transport_type') != 'suburban' or not isinstance(thread.get('stops'), list) or len(
                thread['stops']) < 2:
            raise ValueError(f'Invalid suburban thread: {uid}')
        first_time = thread['stops'][0].get('departure') or thread['stops'][0].get('arrival')
        if not first_time:
            raise ValueError(f'Missing first departure: {uid}')
        service_day = parse_time(first_time).date()
        day_string = service_day.strftime('%Y%m%d')
        service_days.add(day_string)
        suffix = hashlib.sha256(uid.encode()).hexdigest()[:20]
        route_id, trip_id = f'rail:route:{suffix}', f'rail:trip:{suffix}:{day_string}'
        if route_id in route_ids:
            raise ValueError('Rail route ID collision')
        route_ids.add(route_id)
        carrier = thread.get('carrier') or seen[0]['record']['thread'].get('carrier') or {}
        if not carrier.get('code') or not carrier.get('title'):
            raise ValueError(f'Missing rail carrier: {uid}')
        agency_id = f"rail:carrier:{carrier['code']}"
        agency_url = carrier.get('url') or CARRIER_URLS.get(str(carrier['code']))
        if not agency_url or not agency_url.startswith('https://'):
            raise ValueError(f'Missing official carrier URL: {carrier["code"]}')
        agency[agency_id] = {'agency_id': agency_id, 'agency_name': carrier['title'],
                             'agency_url': agency_url, 'agency_timezone': 'Europe/Moscow'}
        number = thread.get('number') or seen[0]['record']['thread'].get('number') or ''
        title = thread.get('title') or seen[0]['record']['thread'].get('title') or ''
        tables['routes.txt'].append({'route_id': route_id, 'agency_id': agency_id,
                                     'route_short_name': number, 'route_long_name': title, 'route_type': 2})
        tables['trips.txt'].append({'route_id': route_id, 'service_id': f'rail:{day_string}',
                                    'trip_id': trip_id, 'trip_headsign': thread['stops'][-1]['station']['title']})
        observed = set()
        last_instant = None
        for sequence, stop in enumerate(thread['stops'], 1):
            station = stop['station']
            code = station['code']
            coords = stations.get(code)
            if not coords:
                raise ValueError(f'Missing station coordinates: {code}')
            lat, lon = float(coords['latitude']), float(coords['longitude'])
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError(f'Invalid station coordinates: {code}')
            stop_id = f'rail:{code}'
            stops[stop_id] = {'stop_id': stop_id, 'stop_name': station['title'],
                              'stop_lat': lat, 'stop_lon': lon}
            arrival = stop.get('arrival') or stop.get('departure')
            departure = stop.get('departure') or stop.get('arrival')
            if not arrival or not departure:
                raise ValueError(f'Missing rail stop time: {uid}/{code}')
            a, d = parse_time(arrival), parse_time(departure)
            if a > d or (last_instant and a < last_instant):
                raise ValueError(f'Non-monotonic rail times: {uid}/{code}')
            last_instant = d
            for event in ('arrival', 'departure'):
                if stop.get(event):
                    observed.add((code, event, parse_time(stop[event])))
            tables['stop_times.txt'].append({'trip_id': trip_id, 'arrival_time': gtfs_time(a, service_day),
                                             'departure_time': gtfs_time(d, service_day),
                                             'stop_id': stop_id, 'stop_sequence': sequence})
        for item in seen:
            value = item['record'].get(item['event'])
            if not value or (item['station'], item['event'], parse_time(value)) not in observed:
                raise ValueError(f'Schedule/thread mismatch: {uid}/{item["station"]}')
    tables['agency.txt'] = list(agency.values())
    tables['stops.txt'] = list(stops.values())
    tables['calendar_dates.txt'] = [{'service_id': f'rail:{day}', 'date': day, 'exception_type': 1}
                                    for day in sorted(service_days)]
    tables['feed_info.txt'] = [{'feed_publisher_name': 'Peresadochnaya',
                                'feed_publisher_url': 'https://github.com/Peresadochnaya/peresadochnaya',
                                'feed_lang': 'ru', 'feed_start_date': min(service_days),
                                'feed_end_date': max(service_days)}]
    write_tables(path, tables)
    return {'routes': len(tables['routes.txt']), 'trips': len(tables['trips.txt']),
            'stops': len(tables['stops.txt']), 'service_dates': sorted(service_days)}


def refresh(root, day, hubs, key, *, get=requests.get, pause=time.sleep,
            stations_file=None, progress=None, raw_dir=None, offline=False, raw_only=False,
            resume=False):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    raw_store = RawStore(Path(raw_dir or root / 'raw') / day.isoformat(),
                         offline=offline, max_age_seconds=None if resume else 3600)
    with store_lock(root):
        schedules = []
        if progress:
            progress.set_stage('Расписания станций', total=len(hubs))
        for station in hubs:
            schedules.extend(collect_schedules(station, day, key=key, get=get,
                                               pause=pause, raw_store=raw_store))
            if progress:
                progress.advance(announce=True)
        uids = sorted({item['record']['thread']['uid'] for item in schedules})
        if progress:
            progress.set_stage(f'Остановки рейсов ({len(schedules)} записей)', total=len(uids))
        observations = {}
        for item in schedules:
            observations.setdefault(item['record']['thread']['uid'], []).append(item)
        threads = {}
        for index, uid in enumerate(uids, 1):
            try:
                threads[uid] = api_get('thread', {'uid': uid, 'date': day.isoformat()},
                                       key=key, get=get, pause=pause, raw_store=raw_store)
            except (requests.HTTPError, FileNotFoundError) as exc:
                if isinstance(exc, requests.HTTPError):
                    if exc.response is None or exc.response.status_code != 404:
                        raise
                elif not offline:
                    raise
                previous = api_get('thread', {'uid': uid, 'date': (day - timedelta(days=1)).isoformat()},
                                   key=key, get=get, pause=pause, raw_store=raw_store)
                if not matches_previous_day_thread(previous, observations[uid], day):
                    raise ValueError(f'Thread for previous day does not match schedule: {uid}/{day}') from exc
                threads[uid] = previous
            if progress:
                progress.advance(announce=index % 25 == 0)
        previous_day = [uid for uid in uids if needs_previous_day_thread(threads[uid], observations[uid])]
        if previous_day and progress:
            progress.set_stage('Ночные рейсы с предыдущего дня', total=len(previous_day))
        for uid in previous_day:
            threads[uid] = api_get('thread', {'uid': uid, 'date': (day - timedelta(days=1)).isoformat()},
                                   key=key, get=get, pause=pause, raw_store=raw_store)
            if progress:
                progress.advance()
        if progress:
            progress.set_stage('Координаты станций', total=1)
        if stations_file:
            station_list = json.loads(Path(stations_file).read_text(encoding='utf-8'))
            if station_list.get('endpoint') == 'stations_list':
                station_list = station_list['response']
        else:
            station_list = api_get('stations_list', {}, key=key, get=get,
                                   pause=pause, raw_store=raw_store)
        if progress:
            progress.advance()
        if raw_only:
            return {'status': 'raw_saved', 'requested_date': day.isoformat(),
                    'requested_stations': list(hubs), 'schedule_records': len(schedules),
                    'unique_threads': len(uids), 'raw_dir': str(raw_store.directory)}
        stations = station_coordinates(station_list)
        if progress:
            progress.set_stage('Сборка и публикация GTFS', total=1)
        with tempfile.TemporaryDirectory(prefix='.rail-', dir=root) as temporary:
            incoming = Path(temporary) / 'feed.zip'
            counts = build_feed(incoming, schedules, threads, stations)
            checked_at = datetime.now(MOSCOW).isoformat()
            result = publish(root, incoming, day.strftime('%Y%m%d'), checked_at)
        version = result['sha256']
        used_codes = {stop['station']['code'] for thread in threads.values() for stop in thread['stops']}
        atomic_json(root / 'versions' / version / 'source.json', {
            'source': 'Yandex Rasp API v3.0', 'requested_date': day.isoformat(),
            'requested_stations': list(hubs), 'retrieved_at': checked_at,
            'schedules': schedules, 'threads': threads,
            'station_coordinates': {code: stations[code] for code in sorted(used_codes)},
        })
        result.update(counts, requested_stations=list(hubs), source='Yandex Rasp API v3.0',
                      coverage='Only trips observed at requested stations; not the entire suburban network')
        atomic_json(root / 'refresh-status.json', result)
        prune(root)
        if progress:
            progress.advance()
        return result


def refresh_range(root, start, days, hubs, key, *, get=requests.get, pause=time.sleep,
                  raw_dir=None, stations_file=None, progress=None, offline=False,
                  raw_only=False, resume=False):
    """Fetch each date independently, then publish one GTFS for the range."""
    if not 1 <= days <= 14:
        raise ValueError('--days должен быть от 1 до 14')
    if days == 1:
        return refresh(root, start, hubs, key, get=get, pause=pause, raw_dir=raw_dir,
                       stations_file=stations_file, progress=progress, offline=offline,
                       raw_only=raw_only, resume=resume)
    from .merge import merge_feeds

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(raw_dir or root / 'raw')
    dates = [start + timedelta(days=offset) for offset in range(days)]
    summaries = []
    with store_lock(root), tempfile.TemporaryDirectory(prefix='.rail-range-', dir=root) as temporary:
        temporary = Path(temporary)
        daily_archives = []
        shared_stations = stations_file
        for index, day in enumerate(dates, 1):
            if progress:
                progress.set_stage(f'Дата {day.isoformat()} ({index}/{days})')
            daily = refresh(temporary / day.isoformat(), day, hubs, key, get=get,
                            pause=pause, raw_dir=raw_dir, stations_file=shared_stations,
                            progress=progress, offline=offline, raw_only=raw_only, resume=resume)
            summaries.append({'date': day.isoformat(), **daily})
            if not raw_only:
                daily_archives.append(temporary / day.isoformat() / 'versions' / daily['sha256'] / 'feed.zip')
            if shared_stations is None:
                shared_stations = RawStore(raw_dir / day.isoformat()).path('stations_list', {})
        requested = [day.isoformat() for day in dates]
        if raw_only:
            return {'status': 'raw_saved', 'requested_dates': requested,
                    'schedule_records': sum(day['schedule_records'] for day in summaries),
                    'daily': [{'date': day['date'], 'schedule_records': day['schedule_records'],
                               'unique_threads': day['unique_threads']} for day in summaries],
                    'raw_dir': str(raw_dir)}
        incoming = temporary / 'combined.zip'
        counts = merge_feeds(daily_archives, incoming)
        checked_at = datetime.now(MOSCOW).isoformat()
        today = datetime.now(MOSCOW).date()
        validation_day = today if dates[0] <= today <= dates[-1] else dates[0]
        result = publish(root, incoming, validation_day.strftime('%Y%m%d'), checked_at)
        version = result['sha256']
        daily_counts = [{'date': day['date'], 'routes': day['routes'],
                         'trips': day['trips'], 'stops': day['stops'],
                         'sha256': day['sha256']} for day in summaries]
        atomic_json(root / 'versions' / version / 'source.json', {
            'source': 'Yandex Rasp API v3.0', 'requested_dates': requested,
            'retrieved_at': checked_at, 'daily': daily_counts,
            'raw_dir': str(raw_dir),
        })
        result.update(counts, requested_dates=requested, daily=daily_counts,
                      source='Yandex Rasp API v3.0',
                      coverage='Only trips observed at requested stations; not the entire suburban network')
        atomic_json(root / 'refresh-status.json', result)
        prune(root)
        return result


def main():
    parser = argparse.ArgumentParser(description='Выгрузить пригородные поезда через API Яндекс.Расписаний')
    parser.add_argument('--date', type=date.fromisoformat, default=datetime.now(MOSCOW).date(),
                        help='дата расписания YYYY-MM-DD; по умолчанию сегодня по Москве')
    parser.add_argument('--days', type=int, default=1,
                        help='число последовательных дат, включая --date; для недели укажите 7')
    parser.add_argument('--station', action='append', dest='stations', help='код станции; можно повторять')
    parser.add_argument('--store-dir', type=Path, default=DATA_DIR / 'processed' / 'rail',
                        help='каталог локального GTFS')
    parser.add_argument('--stations-list', type=Path,
                        help='ранее полученный stations_list JSON вместо сетевого запроса')
    parser.add_argument('--raw-dir', type=Path, default=DATA_DIR / 'raw' / 'rail',
                        help='каталог исходных ответов API')
    parser.add_argument('--offline', action='store_true',
                        help='собрать GTFS только из сохранённых ответов, без API и ключа')
    parser.add_argument('--raw-only', action='store_true',
                        help='получить и сохранить только исходные ответы, не собирать GTFS')
    parser.add_argument('--resume', action='store_true',
                        help='использовать все сохранённые ответы за дату и запросить только недостающие')
    args = parser.parse_args()
    key = os.environ.get('YANDEX_RASP_API_KEY', '').strip()
    if not key and not args.offline:
        parser.error('задайте YANDEX_RASP_API_KEY в окружении')
    try:
        with Progress() as progress:
            result = refresh_range(args.store_dir, args.date, args.days,
                                   tuple(dict.fromkeys(args.stations or HUBS)), key,
                                   stations_file=args.stations_list, progress=progress,
                                   raw_dir=args.raw_dir, offline=args.offline,
                                   raw_only=args.raw_only, resume=args.resume)
    except KeyboardInterrupt:
        print('Выгрузка остановлена пользователем', file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Ошибка выгрузки пригородных поездов: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f'Ошибка получения ответа API: {type(exc).__name__}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
