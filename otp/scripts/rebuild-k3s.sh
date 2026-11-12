#!/usr/bin/env bash
# Explicit manual graph build; use the same kubectl context as the API deployment.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/../../deploy/k8s/otp-build-job.yaml"
NAMESPACE="${NAMESPACE:-peresadochnaya}"
JOB=otp-build
TASK_TMP="$(mktemp -d)"
trap 'rm -rf "$TASK_TMP"' EXIT

# A pending/running job is also active for this purpose; require a terminal condition.
if kubectl -n "$NAMESPACE" get job "$JOB" --ignore-not-found -o json > "$TASK_TMP/previous.json"; then
  if [ -s "$TASK_TMP/previous.json" ]; then
    python3 - "$TASK_TMP/previous.json" <<'PY'
import json, sys
job = json.load(open(sys.argv[1]))
if job.get('metadata', {}).get('labels', {}).get('app') != 'peresadochnaya-otp-build':
    raise SystemExit('Refusing to delete a Job not owned by this build procedure')
terminal = any(c.get('status') == 'True' and c.get('type') in ('Complete', 'Failed')
               for c in job.get('status', {}).get('conditions', []))
if job.get('status', {}).get('active', 0) or not terminal:
    raise SystemExit('OTP build Job is active or pending; refusing concurrent build')
PY
    kubectl -n "$NAMESPACE" delete job "$JOB" --wait=true
  fi
else
  exit 1
fi
IMAGE="$(kubectl -n "$NAMESPACE" get deployment api -o jsonpath='{.spec.template.spec.containers[?(@.name=="api")].image}')"
python3 - "$TEMPLATE" "$IMAGE" "$NAMESPACE" "$TASK_TMP/job.yaml" <<'PY'
from pathlib import Path
import re, sys
source, image, namespace, output = sys.argv[1:]
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]*', image):
    raise SystemExit('Invalid/missing backend image from deployment/api')
if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', namespace):
    raise SystemExit('Invalid namespace')
text = Path(source).read_text().replace('ghcr.io/peresadochnaya/peresadochnaya:REPLACE_WITH_COMMIT_SHA', image)
text = text.replace('namespace: peresadochnaya', 'namespace: ' + namespace)
Path(output).write_text(text)
PY
kubectl -n "$NAMESPACE" apply -f "$TASK_TMP/job.yaml"
if ! kubectl -n "$NAMESPACE" wait --for=condition=complete "job/$JOB" --timeout=1850s; then
  kubectl -n "$NAMESPACE" get job "$JOB"
  echo 'Build failed or timed out; current graph/server were not restarted' >&2
  exit 1
fi
kubectl -n "$NAMESPACE" logs "job/$JOB" -c publish > "$TASK_TMP/result.json"
STATUS="$(python3 - "$TASK_TMP/result.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
status = result.get('status')
if status not in ('updated', 'unchanged'):
    raise SystemExit('Publish did not confirm updated/unchanged graph')
print(status)
PY
)"
cat "$TASK_TMP/result.json"
if [ "$STATUS" = updated ]; then
  kubectl -n "$NAMESPACE" rollout restart deployment/otp
  kubectl -n "$NAMESPACE" rollout status deployment/otp --timeout=600s
else
  echo 'Graph unchanged; OTP restart skipped'
fi

# Confirm the running server loaded current, then record activation metadata.
python3 - "$SCRIPT_DIR/../../deploy/k8s/otp-verify-job.yaml" "$IMAGE" "$NAMESPACE" "$TASK_TMP/verify.yaml" <<'PYVERIFY'
from pathlib import Path
import sys
source, image, namespace, output = sys.argv[1:]
text = Path(source).read_text().replace('ghcr.io/peresadochnaya/peresadochnaya:REPLACE_WITH_COMMIT_SHA', image)
Path(output).write_text(text.replace('namespace: peresadochnaya', 'namespace: ' + namespace))
PYVERIFY
VERIFY_JOB="$(kubectl -n "$NAMESPACE" create -f "$TASK_TMP/verify.yaml" -o jsonpath='{.metadata.name}')"
kubectl -n "$NAMESPACE" wait --for=condition=complete "job/$VERIFY_JOB" --timeout=150s
kubectl -n "$NAMESPACE" logs "job/$VERIFY_JOB"
