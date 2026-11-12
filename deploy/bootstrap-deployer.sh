#!/usr/bin/env bash
# Run with administrator rights ON the k3s node from this repository checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/bootstrap-rbac.yaml
if ! id peresadochnaya >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash peresadochnaya
fi
install -d -o peresadochnaya -g peresadochnaya -m 700 /home/peresadochnaya/.ssh
install -o root -g root -m 700 deploy/renew-deployer-kubeconfig.sh /usr/local/sbin/peresadochnaya-renew-kubeconfig
install -o root -g root -m 644 deploy/peresadochnaya-kubeconfig.service /etc/systemd/system/peresadochnaya-kubeconfig.service
install -o root -g root -m 644 deploy/peresadochnaya-kubeconfig.timer /etc/systemd/system/peresadochnaya-kubeconfig.timer
systemctl daemon-reload
systemctl start peresadochnaya-kubeconfig.service
install -d -o peresadochnaya -g peresadochnaya -m 700 /home/peresadochnaya/.kube
ln -sfn /var/lib/peresadochnaya-deployer/config /home/peresadochnaya/.kube/config
chown -h peresadochnaya:peresadochnaya /home/peresadochnaya/.kube/config
systemctl enable --now peresadochnaya-kubeconfig.timer
echo 'Install the reviewed CI public key as restrict <public-key> in peresadochnaya/.ssh/authorized_keys.'
