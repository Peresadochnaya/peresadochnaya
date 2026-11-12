"""Install the bundled finite rail snapshot only if no newer store exists."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from data.gtfs.versioned_static import publish, store_lock


def seed(bundle: Path, store: Path):
    with store_lock(store):
        if (store / 'current.json').exists():
            return {'status': 'retained'}
        expected = json.loads((bundle / 'current.json').read_text())['sha256']
        feed = bundle / 'feed.zip'
        if hashlib.sha256(feed.read_bytes()).hexdigest() != expected:
            raise ValueError('Bundled rail snapshot digest mismatch')
        now = datetime.now(ZoneInfo('Europe/Moscow'))
        return publish(store, feed, now.strftime('%Y%m%d'), now.isoformat())
