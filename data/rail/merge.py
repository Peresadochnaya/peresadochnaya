"""Merge exact daily rail GTFS archives into one dated feed."""

import csv
import hashlib
import io
import json
import zipfile
from collections import defaultdict

from .refresh import FIELDS, write_tables


def read_rows(archive, name):
    with archive.open(name) as raw:
        return list(csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig')))


def merge_feeds(paths, output):
    if not paths:
        raise ValueError('No daily GTFS archives to merge')
    agencies, stops, routes, route_variants, trips, times, exceptions = {}, {}, {}, {}, {}, {}, {}
    start_dates, end_dates = [], []
    feed_info = None
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            daily = {name: read_rows(archive, name) for name in FIELDS}
        info = daily['feed_info.txt']
        if len(info) != 1:
            raise ValueError(f'Invalid feed_info in {path}')
        if feed_info is None:
            feed_info = dict(info[0])
        elif any(info[0][field] != feed_info[field] for field in
                 ('feed_publisher_name', 'feed_publisher_url', 'feed_lang')):
            raise ValueError('Conflicting daily feed publishers')
        start_dates.append(info[0]['feed_start_date'])
        end_dates.append(info[0]['feed_end_date'])
        if daily['calendar.txt']:
            raise ValueError('Daily rail feed unexpectedly uses calendar.txt')
        for name, collection, key in (('agency.txt', agencies, 'agency_id'),
                                      ('stops.txt', stops, 'stop_id')):
            for row in daily[name]:
                identifier = row[key]
                if identifier in collection and collection[identifier] != row:
                    raise ValueError(f'Conflicting {name}: {identifier}')
                collection[identifier] = row
        route_map = {}
        for row in daily['routes.txt']:
            original = row['route_id']
            variant = (original, tuple((key, row[key]) for key in FIELDS['routes.txt'] if key != 'route_id'))
            target = route_variants.get(variant)
            if target is None:
                if original not in routes:
                    target = original
                else:
                    digest = hashlib.sha256(json.dumps(variant, ensure_ascii=False).encode()).hexdigest()[:12]
                    target = f'{original}:variant:{digest}'
                candidate = {**row, 'route_id': target}
                if target in routes and routes[target] != candidate:
                    raise ValueError(f'Rail route ID collision: {target}')
                routes[target] = candidate
                route_variants[variant] = target
            route_map[original] = target
        for row in daily['trips.txt']:
            mapped = {**row, 'route_id': route_map[row['route_id']]}
            trip_id = mapped['trip_id']
            if trip_id in trips and trips[trip_id] != mapped:
                raise ValueError(f'Conflicting trip across dates: {trip_id}')
            trips[trip_id] = mapped
        grouped = defaultdict(list)
        for row in daily['stop_times.txt']:
            grouped[row['trip_id']].append(row)
        for trip_id, rows in grouped.items():
            if trip_id not in trips:
                raise ValueError(f'Unknown trip in stop_times: {trip_id}')
            if trip_id in times and times[trip_id] != rows:
                raise ValueError(f'Conflicting stop times across dates: {trip_id}')
            times[trip_id] = rows
        for row in daily['calendar_dates.txt']:
            identifier = (row['service_id'], row['date'])
            if identifier in exceptions and exceptions[identifier] != row:
                raise ValueError(f'Conflicting service exception: {identifier}')
            exceptions[identifier] = row
    if any(trip['service_id'] not in {row['service_id'] for row in exceptions.values()}
           for trip in trips.values()):
        raise ValueError('Trip without a service date')
    if any(trip_id not in times for trip_id in trips):
        raise ValueError('Trip without stop times')
    feed_info['feed_start_date'] = min(start_dates)
    feed_info['feed_end_date'] = max(end_dates)
    tables = {'agency.txt': list(agencies.values()), 'stops.txt': list(stops.values()),
              'routes.txt': list(routes.values()), 'trips.txt': list(trips.values()),
              'stop_times.txt': [row for group in times.values() for row in group],
              'calendar.txt': [],
              'calendar_dates.txt': sorted(exceptions.values(), key=lambda row: (row['date'], row['service_id'])),
              'feed_info.txt': [feed_info]}
    write_tables(output, tables)
    return {'routes': len(routes), 'trips': len(trips), 'stops': len(stops),
            'stop_times': len(tables['stop_times.txt']),
            'service_dates': sorted({row['date'] for row in exceptions.values()})}
