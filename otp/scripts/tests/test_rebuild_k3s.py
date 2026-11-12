"""Exercise the operator script against a fake kubectl; never contact a cluster."""
import json
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'rebuild-k3s.sh'
FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TRACE'], 'a') as target:
    target.write(json.dumps(args) + '\n')
args = args[2:] # -n namespace
if args[:3] == ['get', 'deployment', 'api']:
    print('ghcr.io/peresadochnaya/peresadochnaya:test-sha', end='')
elif args[:3] == ['get', 'job', 'otp-build']:
    previous = os.environ['PREVIOUS']
    if previous != 'absent':
        labels = {'app': 'other' if previous == 'foreign' else 'peresadochnaya-otp-build'}
        status = {'active': 1} if previous == 'active' else {'conditions': [{'type': 'Complete', 'status': 'True'}]}
        print(json.dumps({'metadata': {'labels': labels}, 'status': status}))
elif args[0] == 'apply':
    text = Path(args[2]).read_text()
    assert 'REPLACE_WITH_COMMIT_SHA' not in text
    assert 'ghcr.io/peresadochnaya/peresadochnaya:test-sha' in text
elif args[0] == 'create':
    assert 'REPLACE_WITH_COMMIT_SHA' not in Path(args[2]).read_text()
    print('otp-verify-test', end='')
elif args[0] == 'wait' and os.environ['PUBLISH_STATUS'] == 'failed':
    sys.exit(1)
elif args[0] == 'logs':
    print(json.dumps({'status': os.environ['PUBLISH_STATUS'], 'version': 'test-version'}))
'''


@pytest.mark.parametrize('status,previous,restart,exit_ok', [
    ('updated', 'absent', True, True),
    ('unchanged', 'completed', False, True),
    ('failed', 'absent', False, False),
    ('updated', 'active', False, False),
    ('updated', 'foreign', False, False),
])
def test_operator_restarts_only_after_successful_changed_publish(tmp_path, status, previous, restart, exit_ok):
    binary = tmp_path / 'kubectl'
    binary.write_text(FAKE_KUBECTL)
    binary.chmod(0o755)
    trace = tmp_path / 'trace'
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
           'TRACE': str(trace), 'PREVIOUS': previous, 'PUBLISH_STATUS': status}
    result = subprocess.run(['bash', str(SCRIPT)], env=env, text=True, capture_output=True)
    assert (result.returncode == 0) == exit_ok, result.stdout + result.stderr
    calls = [json.loads(line)[2:] for line in trace.read_text().splitlines()]
    assert any(c[:2] == ['rollout', 'restart'] for c in calls) == restart
    assert any(c[0] == 'create' for c in calls) == exit_ok
    assert any(c[:2] == ['delete', 'job'] for c in calls) == (previous == 'completed')
    if previous in ('active', 'foreign'):
        assert not any(c[0] == 'apply' for c in calls)
