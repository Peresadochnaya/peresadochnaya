import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from seed_rail import seed

BUNDLE = Path(__file__).parents[3] / 'data/processed/rail'


def test_bundled_snapshot_is_published_without_network(tmp_path):
    result = seed(BUNDLE, tmp_path)
    digest = result['sha256']
    feed = tmp_path / 'versions' / digest / 'feed.zip'
    assert feed.read_bytes() == (BUNDLE / 'feed.zip').read_bytes()
    assert hashlib.sha256(feed.read_bytes()).hexdigest() == digest
    assert seed(BUNDLE, tmp_path) == {'status': 'retained'}


def test_existing_store_is_not_downgraded_even_if_bundle_missing(tmp_path):
    pointer = tmp_path / 'current.json'
    pointer.write_text('{"sha256":"newer-version"}')
    assert seed(tmp_path / 'missing', tmp_path) == {'status': 'retained'}
    assert json.loads(pointer.read_text())['sha256'] == 'newer-version'


def test_corrupt_bundle_does_not_publish(tmp_path):
    bundle, store = tmp_path / 'bundle', tmp_path / 'store'
    bundle.mkdir(); (bundle / 'feed.zip').write_bytes(b'corrupt')
    (bundle / 'current.json').write_text('{"sha256":"wrong"}')
    with pytest.raises(ValueError, match='digest mismatch'):
        seed(bundle, store)
    assert not (store / 'current.json').exists()
