import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('runtime_router_config', Path(__file__).parents[1] / 'runtime_router_config.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_filters_change_and_graph_config_is_preserved(tmp_path):
    base = tmp_path / 'base.json'
    original = {'configVersion': 'loaded-graph-version', 'updaters': [], 'routingDefaults': {
        'walk': {'speed': 1.3}, 'itineraryFilters': {'groupSimilarityKeepOne': 0.85}}}
    base.write_text(json.dumps(original))
    defaults = Path(__file__).parents[2] / 'config/router-config.json'
    output = tmp_path / 'router-config.json'
    module.configure(base, defaults, output)
    result = json.loads(output.read_text())
    expected = json.loads(base.read_text())
    expected['routingDefaults']['itineraryFilters']['removeItinerariesWithSameRoutesAndStops'] = True
    expected['updaters'] = json.loads(defaults.read_text())['updaters']
    assert result == expected
    assert json.loads(base.read_text()) == original


def test_invalid_filter_settings_fail_before_writing(tmp_path):
    base, defaults, output = [tmp_path / name for name in ('base', 'defaults', 'output')]
    base.write_text('{}')
    defaults.write_text('{"routingDefaults":{"itineraryFilters":[]}}')
    with pytest.raises(ValueError):
        module.configure(base, defaults, output)
    assert not output.exists()


def test_realtime_updater_preserves_other_feeds_and_is_idempotent(tmp_path):
    base,output=tmp_path/'base.json',tmp_path/'output.json'
    defaults=Path(__file__).parents[2]/'config/router-config.json'
    other={'type':'stop-time-updater','feedId':'other','url':'https://example.com/feed'}
    base.write_text(json.dumps({'updaters':[other]}))
    module.configure(base,defaults,output)
    first=json.loads(output.read_text())
    module.configure(output,defaults,base)
    assert json.loads(base.read_text())==first
    assert other in first['updaters'] and len(first['updaters'])==2
