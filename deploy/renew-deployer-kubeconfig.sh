#!/usr/bin/env bash
# Install root-owned at /usr/local/sbin/peresadochnaya-renew-kubeconfig.
set -euo pipefail
umask 077
admin_config=/etc/rancher/k3s/k3s.yaml
target_dir=/var/lib/peresadochnaya-deployer
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT

# Never print token/config or enable shell tracing in this script.
kubectl --kubeconfig "$admin_config" config view --minify --raw -o json > "$staging/admin.json"
kubectl --kubeconfig "$admin_config" -n peresadochnaya create token github-deployer --duration=48h > "$staging/token"
python3 - "$staging" <<'PY'
import base64, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
admin = json.loads((root / 'admin.json').read_text())
cluster = admin['clusters'][0]['cluster']
# Use only public CA + loopback server; do not copy administrator credentials.
config = {
  'apiVersion': 'v1', 'kind': 'Config',
  'clusters': [{'name': 'k3s', 'cluster': {
    'server': 'https://127.0.0.1:6443',
    'certificate-authority-data': cluster['certificate-authority-data']}}],
  'users': [{'name': 'github-deployer', 'user': {'token': (root / 'token').read_text().strip()}}],
  'contexts': [{'name': 'peresadochnaya', 'context': {
    'cluster': 'k3s', 'user': 'github-deployer', 'namespace': 'peresadochnaya'}}],
  'current-context': 'peresadochnaya'}
(root / 'config').write_text(json.dumps(config))
PY
# A root timer must never write through a directory controlled by the SSH user.
# That user reads via ~/.kube/config symlink, but cannot redirect this write.
install -d -o root -g peresadochnaya -m 750 "$target_dir"
install -o root -g peresadochnaya -m 640 "$staging/config" "$target_dir/config.new"
mv "$target_dir/config.new" "$target_dir/config"
echo 'Renewed namespace-scoped deployment credentials.'
