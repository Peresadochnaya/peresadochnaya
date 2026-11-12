from pathlib import Path
import yaml

ROOT = Path(__file__).parents[2]


def test_api_warms_persistent_indexes_before_serving_with_two_cores():
    api = list(yaml.safe_load_all((ROOT / 'deploy/k8s/api.yaml').read_text()))[0]
    spec = api['spec']['template']['spec']
    assert api['spec']['strategy']['rollingUpdate']['maxUnavailable'] == 0
    init = spec['initContainers'][0]
    assert init['command'] == ['python', '-m', 'app.gtfs_index']
    assert '/data/gtfs-index' in init['args']
    container = spec['containers'][0]
    assert container['resources']['limits'] == {'cpu': '2', 'memory': '2Gi'}
    index_mount = next(v for v in container['volumeMounts'] if v['mountPath'] == '/data/gtfs-index')
    assert index_mount['subPath'] == 'gtfs-index' and not index_mount.get('readOnly')
    assert any(v['mountPath'] == '/data' and v.get('readOnly') for v in container['volumeMounts'])
    for feed in ('gtfs', 'metro', 'rail'):
        cron = yaml.safe_load((ROOT / f'deploy/k8s/{feed}-cronjob.yaml').read_text())
        task = cron['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
        assert task['command'] == ['python', '-m', 'app.refresh_gtfs_index']
        assert task['args'][0] == feed
