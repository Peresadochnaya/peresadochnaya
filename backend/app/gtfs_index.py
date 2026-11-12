"""Atomic, versioned SQLite indexes for GTFS ZIPs; no per-request CSV scans."""
import argparse
import csv
from datetime import date
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import zipfile
from contextlib import contextmanager, closing

FORMAT = 1
MAX_TABLE_BYTES = 1024 * 1024 * 1024


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def table_name(name):
    return 'gtfs_' + name.removesuffix('.txt')


def index_directory():
    return Path(os.getenv('GTFS_INDEX_DIR', str(Path(tempfile.gettempdir()) / 'peresadochnaya-gtfs-index')))


def connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA cache_size=-8192')
    return db


def valid(path, digest):
    try:
        with closing(connect(path)) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='source'").fetchone()
            return row is not None and row[0] == digest
    except (OSError, sqlite3.Error):
        return False


def build(archive, path, digest):
    """Build privately with bounded batches, then publish a complete database."""
    fd, temporary = tempfile.mkstemp(prefix='.index-', suffix='.sqlite3', dir=path.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA cache_size=-16384')
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE catalogue (name TEXT PRIMARY KEY)')
            columns = {}
            for info in archive.infolist():
                name = info.filename
                if '/' in name or not name.endswith('.txt'):
                    continue
                if info.file_size > MAX_TABLE_BYTES:
                    raise ValueError('GTFS table is too large')
                with archive.open(info) as raw, io.TextIOWrapper(raw, encoding='utf-8-sig') as text:
                    reader = csv.DictReader(text)
                    fields = reader.fieldnames
                    if not fields or len(fields) != len(set(fields)) or any(not f for f in fields):
                        raise ValueError('Invalid GTFS columns')
                    columns[name] = set(fields)
                    table = quoted(table_name(name))
                    db.execute(f'CREATE TABLE {table} (' + ','.join(quoted(f) + ' TEXT' for f in fields) + ')')
                    db.execute('INSERT INTO catalogue VALUES (?)', (name,))
                    sql = f'INSERT INTO {table} VALUES (' + ','.join('?' for _ in fields) + ')'
                    batch = []
                    for row in reader:
                        if None in row:
                            raise ValueError('Invalid GTFS CSV row')
                        if name == 'calendar_dates.txt':
                            date.fromisoformat(row['date'])
                            if row.get('exception_type') not in ('1', '2'):
                                raise ValueError('Invalid service exception')
                        if name == 'stop_times.txt':
                            int(row['stop_sequence'])
                        batch.append(tuple(row[f] for f in fields))
                        if len(batch) == 1000:
                            db.executemany(sql, batch); batch.clear()
                    if batch:
                        db.executemany(sql, batch)
            for name, keys in [
                ('stop_times.txt', ('stop_id',)),
                ('stop_times.txt', ('trip_id', 'stop_sequence')),
                ('shapes.txt', ('shape_id', 'shape_pt_sequence')),
                ('frequencies.txt', ('trip_id',)),
                ('calendar_dates.txt', ('date',)),
                ('trips.txt', ('trip_id',)),
                ('trips.txt', ('route_id',)),
            ]:
                if set(keys) <= columns.get(name, set()):
                    expression = ','.join('CAST(' + quoted(k) + ' AS INTEGER)' if k.endswith('_sequence') else quoted(k) for k in keys)
                    db.execute(f'CREATE INDEX {quoted(table_name(name) + "_" + "_".join(keys))} ON {quoted(table_name(name))} ({expression})')
            if {'trip_id', 'departure_time', 'stop_sequence'} <= columns.get('stop_times.txt', set()):
                db.execute('''CREATE TABLE first_departures AS
                    SELECT trip_id, departure_time FROM (
                        SELECT trip_id, departure_time,
                        ROW_NUMBER() OVER (PARTITION BY trip_id ORDER BY CAST(stop_sequence AS INTEGER), rowid) AS n
                        FROM gtfs_stop_times) WHERE n=1''')
                db.execute('CREATE UNIQUE INDEX first_departures_trip ON first_departures(trip_id)')
            if 'stop_times.txt' in columns and 'trips.txt' in columns:
                db.execute('''CREATE TABLE stop_routes AS SELECT DISTINCT st.stop_id,t.route_id
                    FROM gtfs_stop_times st LEFT JOIN gtfs_trips t ON t.trip_id=st.trip_id''')
                db.execute('CREATE INDEX stop_routes_stop ON stop_routes(stop_id)')
            db.execute('INSERT INTO metadata VALUES (?,?)', ('source', digest))
            db.execute('PRAGMA user_version=' + str(FORMAT))
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def open_index(archive, digest, directory=None):
    directory = Path(directory) if directory is not None else index_directory()
    directory.mkdir(parents=True, exist_ok=True)
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid GTFS digest')
    path = directory / f'v{FORMAT}-{digest}.sqlite3'
    with (directory / f'{digest}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not valid(path, digest):
            build(archive, path, digest)
        db = connect(path)
    try:
        yield DiskSnapshot(db)
    finally:
        db.close()


class DiskSnapshot:
    def __init__(self, db):
        self.db = db
        self.tables = {row[0] for row in db.execute('SELECT name FROM catalogue')}

    def rows(self, name, required=True, filters=None):
        if name not in self.tables:
            if required:
                raise ValueError('Missing GTFS table')
            return
        sql = 'SELECT * FROM ' + quoted(table_name(name))
        params = ()
        if filters:
            sql += ' WHERE ' + ' AND '.join(quoted(k) + '=?' for k in filters)
            params = tuple(filters.values())
        for row in self.db.execute(sql, params):
            yield dict(row)

    def departures(self, stop_id):
        for row in self.db.execute('''SELECT st.*,f.departure_time AS first_departure
                FROM gtfs_stop_times st JOIN first_departures f ON f.trip_id=st.trip_id
                WHERE st.stop_id=?''', (stop_id,)):
            yield dict(row)

    def frequencies(self, stop_id):
        if 'frequencies.txt' not in self.tables:
            return
        for row in self.db.execute('''SELECT * FROM gtfs_frequencies WHERE trip_id IN
                (SELECT trip_id FROM gtfs_stop_times WHERE stop_id=?)''', (stop_id,)):
            yield dict(row)

    def stop_routes(self):
        return self.db.execute('SELECT stop_id,route_id FROM stop_routes')


def warm(root, directory=None):
    root = Path(root)
    directory = Path(directory) if directory is not None else index_directory()
    active, result = set(), []
    # The loaded OTP graph can use an older static source than current.json.
    # Its index is also consumed by the realtime generator; keep it pinned.
    graph_manifest = root / 'otp/current/manifest.json'
    if graph_manifest.exists():
        graph = json.loads(graph_manifest.read_text())
        digest = graph.get('gtfs_source_sha256', '')
        if len(digest) == 64 and all(c in '0123456789abcdef' for c in digest):
            active.add(f'v{FORMAT}-{digest}.sqlite3')
    for feed in ('gtfs', 'metro', 'rail'):
        pointer = root / feed / 'current.json'
        if not pointer.exists():
            continue
        digest = json.loads(pointer.read_text())['sha256']
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid GTFS pointer')
        source = root / feed / 'versions' / digest / 'feed.zip'
        started = time.monotonic()
        with zipfile.ZipFile(source) as archive, open_index(archive, digest, directory):
            pass
        active.add(f'v{FORMAT}-{digest}.sqlite3')
        result.append({'feed': feed, 'sha256': digest, 'seconds': round(time.monotonic()-started, 3),
                       'index_bytes': (directory / f'v{FORMAT}-{digest}.sqlite3').stat().st_size})
    # Open readonly connections retain their inode; allow a grace period for
    # requests that already opened an old ZIP but have not opened its index yet.
    if directory.exists():
        for path in directory.glob('*.sqlite3'):
            if path.name not in active and path.stat().st_mtime < time.time() - 3600:
                digest = path.stem.split('-', 1)[1]
                with (directory / f'{digest}.lock').open('a') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    path.unlink(missing_ok=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Path('/data'))
    parser.add_argument('--index-dir', type=Path, default=None)
    args = parser.parse_args()
    print(json.dumps(warm(args.data_root, args.index_dir)))
