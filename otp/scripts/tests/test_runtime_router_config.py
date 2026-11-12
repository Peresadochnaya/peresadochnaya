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
    assert result == expected
    assert json.loads(base.read_text()) == original


def test_invalid_filter_settings_fail_before_writing(tmp_path):
    base, defaults, output = [tmp_path / name for name in ('base', 'defaults', 'output')]
    base.write_text('{}')
    defaults.write_text('{"routingDefaults":{"itineraryFilters":[]}}')
    with pytest.raises(ValueError):
        module.configure(base, defaults, output)
    assert not output.exists()
