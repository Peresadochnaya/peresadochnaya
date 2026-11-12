import csv
import io
import zipfile
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from data.rail.merge import merge_feeds
from data.rail.refresh import build_feed, refresh_range


def daily_feed(path, day, *, uid='regular', hour=8, title='А — Б'):
    departure = f'{day}T{hour:02}:00:00+03:00'
    arrival = f'{day}T{hour:02}:10:00+03:00'
    carrier = {'code': 1332, 'title': 'СЗППК'}
    schedules = [{'station': 's1', 'event': 'departure', 'record': {
        'thread': {'uid': uid, 'number': '6307', 'title': title,
                   'transport_type': 'suburban', 'carrier': carrier},
        'departure': departure, 'is_fuzzy': False}}]
    threads = {uid: {'uid': uid, 'number': '6307', 'title': title,
                     'transport_type': 'suburban', 'carrier': carrier,
                     'stops': [
                         {'station': {'code': 's1', 'title': 'А'}, 'departure': departure},
                         {'station': {'code': 's2', 'title': 'Б'}, 'arrival': arrival},
                     ]}}
    stations = {'s1': {'latitude': 59.9, 'longitude': 30.1},
                's2': {'latitude': 60.0, 'longitude': 30.2}}
    build_feed(path, schedules, threads, stations)


def rows(archive, name):
    return list(csv.DictReader(io.TextIOWrapper(archive.open(name), encoding='utf-8')))


class RangeTests(TestCase):
    def test_collects_two_dates_and_publishes_one_feed_offline(self):
        calls = []
        carrier = {'code': 1332, 'title': 'СЗППК'}

        class Response:
            def __init__(self, payload): self.payload = payload
            def raise_for_status(self): pass
            def json(self): return self.payload

        def get(url, *, params, **_):
            endpoint = url.rstrip('/').split('/')[-1]
            calls.append(endpoint)
            if endpoint == 'schedule':
                day = params['date']
                records = ([{'thread': {'uid': 'u1', 'number': '6307', 'title': 'А — Б',
                                        'transport_type': 'suburban', 'carrier': carrier},
                             'departure': f'{day}T08:00:00+03:00', 'is_fuzzy': False}]
                           if params['event'] == 'departure' else [])
                return Response({'station': {'code': 's1'}, 'date': day,
                                 'pagination': {'total': len(records), 'offset': 0, 'limit': 100},
                                 'schedule': records, 'interval_schedule': []})
            if endpoint == 'thread':
                day = params['date']
                return Response({'uid': 'u1', 'transport_type': 'suburban', 'number': '6307',
                                 'title': 'А — Б', 'carrier': carrier, 'stops': [
                                     {'station': {'code': 's1', 'title': 'А'},
                                      'departure': f'{day} 08:00:00'},
                                     {'station': {'code': 's2', 'title': 'Б'},
                                      'arrival': f'{day} 08:10:00'},
                                 ]})
            return Response({'countries': [{'regions': [{'settlements': [{'stations': [
                {'codes': {'yandex_code': 's1'}, 'latitude': 59.9, 'longitude': 30.1},
                {'codes': {'yandex_code': 's2'}, 'latitude': 60.0, 'longitude': 30.2},
            ]}]}]}]})

        with TemporaryDirectory() as directory:
            root = Path(directory)
            args = (root / 'processed', date(2026, 11, 12), 2, ('s1',), 'secret')
            raw_result = refresh_range(*args, get=get, pause=lambda _: None,
                                       raw_dir=root / 'raw', raw_only=True)
            self.assertEqual(raw_result['status'], 'raw_saved')
            self.assertEqual(calls.count('stations_list'), 1)
            before = len(calls)
            result = refresh_range(*args, get=get, pause=lambda _: None,
                                   raw_dir=root / 'raw', offline=True)
            self.assertEqual(len(calls), before)
            self.assertEqual(result['requested_dates'], ['2026-11-12', '2026-11-13'])
            self.assertEqual([item['trips'] for item in result['daily']], [1, 1])
            feed = root / 'processed' / 'versions' / result['sha256'] / 'feed.zip'
            with zipfile.ZipFile(feed) as archive:
                self.assertEqual(len(rows(archive, 'trips.txt')), 2)
                self.assertEqual([r['date'] for r in rows(archive, 'calendar_dates.txt')],
                                 ['20261112', '20261113'])
            with self.assertRaises(FileNotFoundError):
                refresh_range(root / 'processed', date(2026, 11, 12), 3, ('s1',), '',
                              get=get, pause=lambda _: None, raw_dir=root / 'raw', offline=True)
            import json
            self.assertEqual(json.loads((root / 'processed' / 'current.json').read_text())['sha256'],
                             result['sha256'])

    def test_combines_distinct_day_times_into_one_calendar(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            daily_feed(root / 'first.zip', '2026-11-12', hour=8)
            daily_feed(root / 'second.zip', '2026-11-13', hour=9)
            counts = merge_feeds([root / 'first.zip', root / 'second.zip'], root / 'week.zip')
            with zipfile.ZipFile(root / 'week.zip') as archive:
                self.assertEqual(len(rows(archive, 'routes.txt')), 1)
                self.assertEqual(len(rows(archive, 'trips.txt')), 2)
                self.assertEqual([r['date'] for r in rows(archive, 'calendar_dates.txt')],
                                 ['20261112', '20261113'])
                self.assertEqual({r['departure_time'] for r in rows(archive, 'stop_times.txt')},
                                 {'08:00:00', '08:10:00', '09:00:00', '09:10:00'})
                self.assertEqual(rows(archive, 'feed_info.txt')[0]['feed_end_date'], '20261113')
            self.assertEqual(counts['trips'], 2)

    def test_same_overnight_trip_is_not_added_twice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            daily_feed(root / 'first.zip', '2026-11-12')
            daily_feed(root / 'second.zip', '2026-11-12')
            merge_feeds([root / 'first.zip', root / 'second.zip'], root / 'week.zip')
            with zipfile.ZipFile(root / 'week.zip') as archive:
                self.assertEqual(len(rows(archive, 'trips.txt')), 1)
                self.assertEqual(len(rows(archive, 'stop_times.txt')), 2)

    def test_changed_route_title_keeps_both_day_trips(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            daily_feed(root / 'first.zip', '2026-11-12', title='А — Б')
            daily_feed(root / 'second.zip', '2026-11-13', title='А — В')
            merge_feeds([root / 'first.zip', root / 'second.zip'], root / 'week.zip')
            with zipfile.ZipFile(root / 'week.zip') as archive:
                routes = rows(archive, 'routes.txt')
                trips = rows(archive, 'trips.txt')
                self.assertEqual(len(routes), 2)
                self.assertEqual({r['route_id'] for r in routes}, {t['route_id'] for t in trips})
