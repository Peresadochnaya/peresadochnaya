"""Combined pipeline regressions; no network, Docker, Kubernetes or OTP process."""
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import graph_store as graph
import prepare_combined as combined


def feed(path, *, end='20191230', marker='surface'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('calendar.txt', 'service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n'
                         f'1,1,1,1,1,1,1,1,20260801,{end}\n')
        archive.writestr('calendar_dates.txt', 'service_id,date,exception_type\n1,20291109,1\n')
        archive.writestr('marker.txt', marker)
    return path


def publish_feed(root, name, marker, *, end='20191230'):
    source = feed(root / (name + '.zip'), marker=marker, end=end)
    sha = graph.sha256_file(source)
    target = root / name / 'versions' / sha / 'feed.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    graph.atomic_json(root / name / 'current.json', {'sha256': sha})
    return target, sha


@pytest.fixture
def data(tmp_path):
    osm = tmp_path / 'otp/osm/spb-lo.osm.pbf'
    osm.parent.mkdir(parents=True)
    osm.write_bytes(b'test osm')
    config = tmp_path / 'config'
    config.mkdir()
    for name in graph.CONFIG_FILES:
        graph.atomic_json(config / name, {'configVersion': 'local'})
    surface, _ = publish_feed(tmp_path, 'gtfs', 'surface')
    metro, _ = publish_feed(tmp_path, 'metro', 'metro', end='20291109')
    return tmp_path, config, surface, metro


def finish_build(root):
    (root / 'otp/staging/graph.obj').write_bytes(b'valid graph content')
    return combined.publish(root)


def test_both_sources_provenance_and_only_surface_staging_calendar_repaired(data):
    root, config, surface, metro = data
    surface_before, metro_before = surface.read_bytes(), metro.read_bytes()
    result = combined.prepare(root, config)
    assert result['status'] == 'prepared'
    manifest = graph.read_json(root / 'otp/staging/manifest.prepared.json')
    assert manifest['gtfs_sources'] == {'surface': graph.sha256_file(surface), 'metro': graph.sha256_file(metro)}
    assert manifest['metro_source_sha256'] == graph.sha256_file(metro)
    assert manifest['runtime_image'] == combined.OTP_IMAGE_DIGEST
    assert manifest['otp_version'] == combined.OTP_VERSION
    assert manifest['otp_serialization_version_id'] == combined.OTP_SERIAL
    assert manifest['gtfs_calendar_fixed']
    assert surface.read_bytes() == surface_before and metro.read_bytes() == metro_before
    staging = root / 'otp/staging'
    assert graph.sha256_file(staging / 'feed.gtfs.zip') != graph.sha256_file(surface)
    assert (staging / 'metro.gtfs.zip').read_bytes() == metro_before
    assert (staging / 'spb-lo.osm.pbf').is_symlink()
    assert (staging / 'spb-lo.osm.pbf').readlink() == root / 'otp/osm/spb-lo.osm.pbf'
    assert graph.read_json(staging / 'router-config.json')['configVersion'] == result['version']


def test_same_inputs_skip_build_and_publish_without_touching_current(data):
    root, config, _, _ = data
    combined.prepare(root, config)
    assert finish_build(root)['status'] == 'updated'
    current = root / 'otp/current.json'
    before = current.read_bytes()
    version = graph.read_json(current)['version']
    result = combined.prepare(root, config)
    assert result['status'] == 'unchanged'
    assert (root / 'otp/staging/.skip-build').read_text() == version
    assert combined.publish(root) == {'status': 'unchanged', 'version': version}
    assert current.read_bytes() == before


@pytest.mark.parametrize('changed', ['gtfs', 'metro', 'config', 'osm'])
def test_each_snapshot_or_config_change_requires_new_build(data, changed):
    root, config, _, _ = data
    first = combined.prepare(root, config)
    finish_build(root)
    if changed in ('gtfs', 'metro'):
        publish_feed(root, changed, 'changed source', end='20291109')
    elif changed == 'config':
        graph.atomic_json(config / 'router-config.json', {'configVersion': 'local', 'routingDefaults': {}})
    else:
        (root / 'otp/osm/spb-lo.osm.pbf').write_bytes(b'changed osm')
    new = combined.prepare(root, config)
    assert new['status'] == 'prepared' and new['version'] != first['version']
    assert not (root / 'otp/staging/.skip-build').exists()


def test_failed_build_leaves_current_graph_and_metadata_untouched(data):
    root, config, _, _ = data
    combined.prepare(root, config)
    finish_build(root)
    before = (root / 'otp/current.json').read_bytes()
    link_before = (root / 'otp/current').readlink()
    publish_feed(root, 'metro', 'new metro', end='20291109')
    combined.prepare(root, config)
    with pytest.raises(SystemExit, match='graph.obj'):
        combined.publish(root)
    assert (root / 'otp/current.json').read_bytes() == before
    assert (root / 'otp/current').readlink() == link_before


def test_metro_hash_mismatch_refuses_build_without_changing_current(data):
    root, config, _, metro = data
    combined.prepare(root, config)
    finish_build(root)
    before = (root / 'otp/current.json').read_bytes()
    metro.write_bytes(b'corrupted snapshot')
    with pytest.raises(SystemExit, match='digest mismatch'):
        combined.prepare(root, config)
    assert (root / 'otp/current.json').read_bytes() == before


def test_historical_surface_only_version_does_not_prevent_combined_staging(data):
    root, config, _, _ = data
    first = combined.prepare(root, config)
    manifest = graph.read_json(root / 'otp/staging/manifest.prepared.json')
    surface_version = graph.graph_version(manifest['gtfs_fixed_sha256'], manifest['osm_sha256'],
        manifest['config_sha256'], combined.OTP_VERSION, combined.OTP_SERIAL)
    old = root / 'otp/versions' / surface_version
    old.mkdir(parents=True)
    (old / 'graph.obj').write_bytes(b'old surface only graph')
    second = combined.prepare(root, config)
    assert second['status'] == 'prepared' and second['version'] == first['version']
    assert (root / 'otp/staging/metro.gtfs.zip').exists()


def test_native_surface_only_hash_matches_initial_combined_formula():
    values = ('a'*64, 'b'*64, 'c'*64, '2.11.0-SNAPSHOT', '289')
    expected = graph.sha256_json({'pipeline_version': 1, 'gtfs_fixed_sha256': values[0],
        'osm_sha256': values[1], 'config_sha256': values[2], 'otp_version': values[3],
        'otp_serialization_version_id': values[4]})[:16]
    assert graph.graph_version(*values) == expected
    assert graph.graph_version(*values, metro_sha='d'*64) != expected
