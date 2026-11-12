from pathlib import Path
import importlib
import sys

import pytest

from app import refresh_gtfs_index


@pytest.mark.parametrize('feed,module_name', [('gtfs', 'data.gtfs.versioned_static'), ('metro', 'data.metro.refresh')])
def test_refresh_then_warm_without_changing_source_updaters(tmp_path, monkeypatch, feed, module_name):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2]))
    updater = importlib.import_module(module_name)
    calls = []
    monkeypatch.setattr(updater, 'refresh', lambda root: calls.append(('refresh', root)) or {'status': 'unchanged'})
    monkeypatch.setattr(refresh_gtfs_index, 'warm', lambda root: calls.append(('warm', root)) or [])
    monkeypatch.setattr(sys, 'argv', ['refresh_gtfs_index', feed, '--data-root', str(tmp_path)])
    refresh_gtfs_index.main()
    assert calls == [('refresh', tmp_path / feed), ('warm', tmp_path)]


def test_rail_refresh_uses_rolling_week_and_never_logs_key(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2]))
    import data.rail.refresh as updater
    calls = []
    monkeypatch.setattr(updater, 'refresh_range', lambda *args, **kw: calls.append((args, kw)) or {'status': 'updated'})
    monkeypatch.setattr(refresh_gtfs_index, 'warm', lambda root: [])
    monkeypatch.setenv('YANDEX_RASP_API_KEY', 'test-only-not-a-real-key')
    monkeypatch.setattr(sys, 'argv', ['refresh_gtfs_index', 'rail', '--data-root', str(tmp_path)])
    refresh_gtfs_index.main()
    args, kwargs = calls[0]
    assert args[0] == tmp_path / 'rail' and args[2] == 7
    assert kwargs['raw_dir'] == tmp_path / 'rail-raw'
    assert 'test-only-not-a-real-key' not in capsys.readouterr().out
