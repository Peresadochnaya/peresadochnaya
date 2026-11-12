#!/usr/bin/env python3
"""Prepare/publish a graph from surface, metro and dated suburban rail GTFS."""
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import graph_store as graph

OTP_VERSION = '2.11.0-SNAPSHOT'
OTP_SERIAL = '289'
OTP_IMAGE_DIGEST = 'sha256:9840beed08759cffac9be01f83e9fad89b8123f8fbce7890a601cc6581c1fa59'


def combined_version(surface_version, metro_sha, rail_sha):
    # Preserve the version formula used for the initial deployed graph.
    return hashlib.sha256(json.dumps({
        'surface_version': surface_version, 'metro_sha256': metro_sha, 'rail_sha256': rail_sha,
        'runtime_image': OTP_IMAGE_DIGEST,
    }, sort_keys=True).encode()).hexdigest()[:16]


def valid_current(root, version):
    current = graph.read_json(root / 'current.json')
    path = root / 'versions' / version / 'graph.obj'
    return (current.get('version') == version and path.is_file()
            and path.stat().st_size > 0
            and current.get('graph_sha256') == graph.sha256_file(path)
            and (root / 'current').is_symlink()
            and (root / 'current').resolve() == path.parent.resolve())


def resolve_rail(processed_dir):
    """Read the published, dated rail GTFS snapshot and verify its digest."""
    processed_dir = Path(processed_dir)
    source = processed_dir / 'feed.zip'
    pointer = graph.read_json(processed_dir / 'current.json')
    if not source.is_file():
        raise SystemExit(f'Rail GTFS feed is missing: {source}')
    sha = graph.sha256_file(source)
    if pointer.get('sha256') != sha:
        raise SystemExit('Rail snapshot digest mismatch')
    return source, sha


def prepare(data_root, config_dir):
    data_root, config_dir = Path(data_root).resolve(), Path(config_dir).resolve()
    root = data_root / 'otp'
    # Force only the original surface preparer: it must always create staging,
    # even if a historical surface-only graph already exists. It never publishes.
    args = SimpleNamespace(data_dir=root, config_dir=config_dir,
        osm_pbf=root / 'osm/spb-lo.osm.pbf', gtfs_zip=None,
        gtfs_store=data_root / 'gtfs', otp_version=OTP_VERSION,
        otp_serialization_version_id=OTP_SERIAL, force=True, metro_zip=None, metro_store=None, no_metro=True)
    with redirect_stdout(io.StringIO()):
        graph.prepare(args)
    staging = root / 'staging'
    with graph.build_lock(root):
        manifest = graph.read_json(staging / 'manifest.prepared.json')
        source, sha = graph.resolve_gtfs(None, data_root / 'metro')
        target = staging / 'metro.gtfs.zip'
        shutil.copyfile(source, target)
        if graph.sha256_file(target) != sha:
            raise SystemExit('Metro snapshot digest mismatch')
        rail_source, rail_sha = resolve_rail(data_root / 'processed' / 'rail')
        rail_target = staging / 'rail.gtfs.zip'
        shutil.copyfile(rail_source, rail_target)
        if graph.sha256_file(rail_target) != rail_sha:
            raise SystemExit('Rail snapshot digest mismatch')
        version = combined_version(manifest['version'], sha, rail_sha)
        manifest.update(version=version, metro_source_sha256=sha, rail_source_sha256=rail_sha,
            runtime_image=OTP_IMAGE_DIGEST,
            gtfs_sources={'surface': manifest['gtfs_source_sha256'], 'metro': sha, 'rail': rail_sha},
            warnings=[
                'Surface invalid calendar end dates repaired only in graph input copy; not authoritative service availability.',
                'Metro uses approximate pMetro running times and modelled headways, not exact departures.',
                'Rail timetable is a dated snapshot; refresh it before its published service period ends.'])
        graph.write_configs(staging, graph.load_configs(config_dir), version)
        graph.atomic_json(staging / 'manifest.prepared.json', manifest)
        # Absolute link works both through /data and a /var/opentripplanner subPath.
        os.symlink(str(root / 'osm/spb-lo.osm.pbf'), staging / 'spb-lo.osm.pbf')
        if valid_current(root, version):
            (staging / '.skip-build').write_text(version)
            status = 'unchanged'
        else:
            status = 'prepared'
            # Original publish cannot replace an existing version safely. A corrupt
            # current graph must not be announced as successfully rebuilt.
            if (root / 'versions' / version).exists():
                raise SystemExit('Target graph version exists but is not valid/current; inspect it before rebuilding')
        graph.update_build_status(root, {'status': status, 'version': version, 'error': None})
        return {'status': status, 'version': version, 'gtfs_sources': manifest['gtfs_sources']}


def publish(data_root):
    root = Path(data_root).resolve() / 'otp'
    marker = root / 'staging/.skip-build'
    if marker.is_file():
        version = marker.read_text().strip()
        if not valid_current(root, version):
            raise SystemExit('Current graph changed after unchanged preparation; refusing to publish')
        return {'status': 'unchanged', 'version': version}
    # Original publisher checks graph.obj is nonempty before touching current.
    with redirect_stdout(io.StringIO()) as output:
        graph.publish(SimpleNamespace(data_dir=root))
    return json.loads(output.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'publish'])
    parser.add_argument('--data-root', type=Path, default=Path('/data'))
    parser.add_argument('--config-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'config')
    args = parser.parse_args()
    result = prepare(args.data_root, args.config_dir) if args.action == 'prepare' else publish(args.data_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
