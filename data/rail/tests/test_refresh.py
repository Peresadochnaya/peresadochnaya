import csv
import io
import unittest
import zipfile
from datetime import date, timedelta

from data.rail.refresh import Progress, RawStore, build_feed, collect_schedules, parse_time, refresh


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class RefreshTests(unittest.TestCase):
    def test_resume_reuses_old_saved_response(self):
        import os
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            store = RawStore(Path(directory), max_age_seconds=None)
            params = {'uid': 'night', 'date': '2026-11-12'}
            store.write('thread', params, {'uid': 'night'})
            os.utime(store.path('thread', params), (1, 1))
            self.assertEqual(store.read('thread', params), {'uid': 'night'})

    def test_naive_thread_time_uses_moscow_timezone(self):
        instant = parse_time('2026-11-12 15:04:00')
        self.assertEqual(instant.utcoffset(), timedelta(hours=3))
        self.assertEqual(instant.isoformat(), '2026-11-12T15:04:00+03:00')

    def test_progress_shows_completed_and_remaining_time(self):
        elapsed = [0.0]
        messages = []
        progress = Progress(clock=lambda: elapsed[0], emit=messages.append)
        progress.set_stage('Загрузка рейсов', total=100)
        elapsed[0] = 20.0
        progress.advance(10, announce=True)
        self.assertIn('10/100', messages[-1])
        self.assertIn('03:00', messages[-1])

    def test_schedule_pages_and_station_events(self):
        calls = []

        def get(url, *, params, **_):
            calls.append((params['event'], params['offset']))
            offset = params['offset']
            items = [{'thread': {'uid': str(offset)}, 'departure': '2026-11-12T08:00:00+03:00'}]
            return Response({'station': {'code': 's1'}, 'date': '2026-11-12',
                             'pagination': {'total': 2, 'offset': offset, 'limit': 1},
                             'schedule': items, 'interval_schedule': []})

        result = collect_schedules('s1', date(2026, 11, 12), get=get, pause=lambda _: None)
        self.assertEqual(len(result), 4)
        self.assertEqual(calls, [('departure', 0), ('departure', 1), ('arrival', 0), ('arrival', 1)])


    def test_reject_ignored_pagination(self):
        def get(url, *, params, **_):
            return Response({'station': {'code': 's1'}, 'date': '2026-11-12',
                             'pagination': {'total': 101, 'offset': 0, 'limit': 100},
                             'schedule': [{}] * 100, 'interval_schedule': []})

        with self.assertRaisesRegex(ValueError, 'pagination|unsupported'):
            collect_schedules('s1', date(2026, 11, 12), get=get, pause=lambda _: None)


    def test_build_gtfs_from_dated_thread(self):
        from tempfile import TemporaryDirectory
        original = '2026-11-12T23:58:00+03:00'
        next_day = '2026-11-13T00:07:00+03:00'
        schedules = [{'station': 's1', 'event': 'departure', 'record': {
            'thread': {'uid': 'u1', 'number': '6307', 'title': 'А — Б',
                       'transport_type': 'suburban', 'carrier': {'code': 1332, 'title': 'СЗППК'}},
            'departure': original, 'is_fuzzy': False}}]
        threads = {'u1': {'uid': 'u1', 'transport_type': 'suburban', 'number': '6307',
                          'title': 'А — Б', 'carrier': {'code': 1332, 'title': 'СЗППК'},
                          'stops': [
                              {'station': {'code': 's1', 'title': 'А'}, 'departure': original, 'arrival': None},
                              {'station': {'code': 's2', 'title': 'Б'}, 'arrival': next_day, 'departure': None},
                          ]}}
        stations = {'s1': {'latitude': 59.9, 'longitude': 30.1},
                    's2': {'latitude': 60.0, 'longitude': 30.2}}
        with TemporaryDirectory() as directory:
            from pathlib import Path
            target = Path(directory) / 'feed.zip'
            build_feed(target, schedules, threads, stations)
            with zipfile.ZipFile(target) as archive:
                def rows(name):
                    return list(csv.DictReader(io.TextIOWrapper(archive.open(name), encoding='utf-8')))
                self.assertEqual(rows('routes.txt')[0]['route_type'], '2')
                self.assertEqual(rows('agency.txt')[0]['agency_url'], 'https://ppk-piter.ru/')
                self.assertEqual(rows('calendar_dates.txt'), [
                    {'service_id': 'rail:20261112', 'date': '20261112', 'exception_type': '1'}])
                self.assertEqual(rows('stop_times.txt')[1]['arrival_time'], '24:07:00')
                self.assertEqual(rows('stops.txt')[0]['stop_lat'], '59.9')


    def test_empty_schedule_prevents_feed(self):
        with self.assertRaisesRegex(ValueError, 'No suburban schedules'):
            build_feed('unused.zip', [], {}, {})

    def test_refresh_publishes_versioned_gtfs(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        original = '2026-11-12T08:00:00+03:00'
        arrival = '2026-11-12T08:10:00+03:00'
        thread = {'uid': 'u1', 'transport_type': 'suburban', 'number': '6307',
                  'title': 'А — Б', 'carrier': {'code': 1332, 'title': 'СЗППК'},
                  'stops': [
                      {'station': {'code': 's1', 'title': 'А'}, 'departure': original},
                      {'station': {'code': 's2', 'title': 'Б'}, 'arrival': arrival},
                  ]}

        def get(url, *, params, **_):
            if '/schedule/' in url:
                event = params['event']
                rows = ([{'thread': {'uid': 'u1', 'transport_type': 'suburban',
                                     'carrier': thread['carrier']},
                          'departure': original, 'is_fuzzy': False}]
                        if event == 'departure' else [])
                return Response({'station': {'code': 's1'}, 'date': '2026-11-12',
                                 'pagination': {'total': len(rows), 'offset': 0, 'limit': 100},
                                 'schedule': rows, 'interval_schedule': []})
            if '/thread/' in url:
                return Response(thread)
            return Response({'countries': [{'regions': [{'settlements': [{'stations': [
                {'codes': {'yandex_code': 's1'}, 'latitude': 59.9, 'longitude': 30.1},
                {'codes': {'yandex_code': 's2'}, 'latitude': 60.0, 'longitude': 30.2},
            ]}]}]}]})

        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = refresh(root, date(2026, 11, 12), ('s1',), 'fake-key', get=get, pause=lambda _: None)
            self.assertEqual(result['status'], 'updated')
            self.assertEqual(result['active_service_ids'], 1)
            feed = root / 'versions' / result['sha256'] / 'feed.zip'
            self.assertTrue(feed.is_file())
            self.assertTrue((root / 'versions' / result['sha256'] / 'source.json').is_file())
            self.assertEqual(__import__('json').loads((root / 'current.json').read_text())['sha256'], result['sha256'])

    def test_raw_responses_survive_feed_failure_and_can_be_reused_offline(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        calls = []

        def get(url, *, params, **_):
            calls.append(url)
            if '/schedule/' in url:
                records = ([{'thread': {'uid': 'u1', 'transport_type': 'suburban',
                                        'carrier': {'code': 1332, 'title': 'СЗППК'}},
                             'departure': '2026-11-12T08:00:00+03:00', 'is_fuzzy': False}]
                           if params['event'] == 'departure' else [])
                return Response({'station': {'code': 's1'}, 'date': '2026-11-12',
                                 'pagination': {'total': len(records), 'offset': 0, 'limit': 100},
                                 'schedule': records, 'interval_schedule': []})
            if '/thread/' in url:
                return Response({'uid': 'u1', 'transport_type': 'suburban',
                                 'carrier': {'code': 1332, 'title': 'СЗППК'},
                                 'stops': [
                                     {'station': {'code': 's1', 'title': 'А'},
                                      'departure': '2026-11-12T08:00:00+03:00'},
                                     {'station': {'code': 's2', 'title': 'Б'},
                                      'arrival': '2026-11-12T08:10:00+03:00'},
                                 ]})
            return Response({'countries': []})

        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw'
            day = date(2026, 11, 12)
            with self.assertRaisesRegex(ValueError, 'Missing station coordinates'):
                refresh(root / 'processed', day, ('s1',), 'secret-key', get=get,
                        pause=lambda _: None, raw_dir=raw)
            saved = list(raw.rglob('*.json'))
            self.assertEqual(len(saved), 4)
            self.assertTrue(any(json.loads(path.read_text())['endpoint'] == 'thread' for path in saved))
            self.assertNotIn('secret-key', ''.join(path.read_text() for path in saved))
            self.assertFalse((root / 'processed' / 'current.json').exists())
            first_call_count = len(calls)
            raw_result = refresh(root / 'processed', day, ('s1',), '', get=get,
                                 pause=lambda _: None, raw_dir=raw, offline=True, raw_only=True)
            self.assertEqual(raw_result['status'], 'raw_saved')
            self.assertEqual(raw_result['unique_threads'], 1)
            self.assertFalse((root / 'processed' / 'current.json').exists())
            with self.assertRaisesRegex(ValueError, 'Missing station coordinates'):
                refresh(root / 'processed', day, ('s1',), '', get=get,
                        pause=lambda _: None, raw_dir=raw, offline=True)
            self.assertEqual(len(calls), first_call_count)

    def test_overnight_arrival_uses_previous_day_thread(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        requested = date(2026, 11, 12)
        calls = []

        def get(url, *, params, **_):
            calls.append((url.rsplit('/', 2)[-2], params.copy()))
            if '/schedule/' in url:
                rows = ([{'thread': {'uid': 'night', 'transport_type': 'suburban',
                                     'carrier': {'code': 1332, 'title': 'СЗППК'}},
                          'arrival': '2026-11-12T00:04:00+03:00', 'is_fuzzy': False}]
                        if params['event'] == 'arrival' else [])
                return Response({'station': {'code': 's2'}, 'date': '2026-11-12',
                                 'pagination': {'total': len(rows), 'offset': 0, 'limit': 100},
                                 'schedule': rows, 'interval_schedule': []})
            if '/thread/' in url:
                day = params['date']
                return Response({'uid': 'night', 'transport_type': 'suburban', 'number': '6386',
                                 'title': 'А — Б', 'carrier': {'code': 1332, 'title': 'СЗППК'},
                                 'stops': [
                                     {'station': {'code': 's1', 'title': 'А'},
                                      'departure': f'{day} 23:50:00'},
                                     {'station': {'code': 's2', 'title': 'Б'},
                                      'arrival': f'{date.fromisoformat(day) + timedelta(days=1)} 00:04:00'},
                                 ]})
            return Response({'countries': [{'regions': [{'settlements': [{'stations': [
                {'codes': {'yandex_code': 's1'}, 'latitude': 59.9, 'longitude': 30.1},
                {'codes': {'yandex_code': 's2'}, 'latitude': 60.0, 'longitude': 30.2},
            ]}]}]}]})

        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw'
            result = refresh(root / 'processed', requested, ('s2',), 'key', get=get,
                             pause=lambda _: None, raw_dir=raw, raw_only=True)
            self.assertEqual(result['status'], 'raw_saved')
            dates = [params['date'] for endpoint, params in calls if endpoint == 'thread']
            self.assertEqual(dates, ['2026-11-12', '2026-11-11'])
            result = refresh(root / 'processed', requested, ('s2',), '', get=get,
                             pause=lambda _: None, raw_dir=raw, offline=True)
            self.assertEqual(result['service_dates'], ['20261111'])

    def test_404_for_overnight_arrival_uses_verified_previous_day_thread_offline_too(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import requests

        requested = date(2026, 11, 14)
        calls = []

        def get(url, *, params, **_):
            endpoint = url.rstrip('/').split('/')[-1]
            if endpoint == 'schedule':
                rows = ([{'thread': {'uid': '6386', 'transport_type': 'suburban',
                                     'carrier': {'code': 1332, 'title': 'СЗППК'}},
                          'arrival': '2026-11-14T00:04:00+03:00', 'is_fuzzy': False}]
                        if params['event'] == 'arrival' else [])
                return Response({'station': {'code': 's2'}, 'date': requested.isoformat(),
                                 'pagination': {'total': len(rows), 'offset': 0, 'limit': 100},
                                 'schedule': rows, 'interval_schedule': []})
            if endpoint == 'thread':
                calls.append(params['date'])
                if params['date'] == requested.isoformat():
                    missing = requests.Response()
                    missing.status_code = 404
                    missing.url = url
                    return missing
                return Response({'uid': '6386', 'transport_type': 'suburban', 'number': '6386',
                                 'title': 'А — Б', 'carrier': {'code': 1332, 'title': 'СЗППК'},
                                 'stops': [
                                     {'station': {'code': 's1', 'title': 'А'},
                                      'departure': '2026-11-13 22:36:00'},
                                     {'station': {'code': 's2', 'title': 'Б'},
                                      'arrival': '2026-11-14 00:04:00'},
                                 ]})
            return Response({'countries': [{'regions': [{'settlements': [{'stations': [
                {'codes': {'yandex_code': 's1'}, 'latitude': 59.9, 'longitude': 30.1},
                {'codes': {'yandex_code': 's2'}, 'latitude': 60.0, 'longitude': 30.2},
            ]}]}]}]})

        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / 'raw'
            result = refresh(root / 'processed', requested, ('s2',), 'key', get=get,
                             pause=lambda _: None, raw_dir=raw, raw_only=True)
            self.assertEqual(result['status'], 'raw_saved')
            self.assertEqual(calls, ['2026-11-14', '2026-11-13'])
            result = refresh(root / 'processed', requested, ('s2',), '', get=get,
                             pause=lambda _: None, raw_dir=raw, offline=True)
            self.assertEqual(result['service_dates'], ['20261113'])
            self.assertEqual(calls, ['2026-11-14', '2026-11-13'])


if __name__ == '__main__':
    unittest.main()
