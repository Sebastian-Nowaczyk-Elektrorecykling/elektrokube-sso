#!/usr/bin/env bash
# CI-only Linux amd64 tools; every archive is checked before extraction.
set -Eeuo pipefail
mkdir -p .cache/bin
cd .cache/bin
fetch() {
  local url=$1 archive=$2 digest=$3 member=$4
  curl -fsSL --retry 3 --connect-timeout 15 --max-time 120 "$url" -o "$archive"
  printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
  tar --no-same-owner -xzf "$archive" "$member"
}
fetch https://get.helm.sh/helm-v4.3.0-linux-amd64.tar.gz helm.tgz 86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb linux-amd64/helm
mv linux-amd64/helm helm
fetch https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize/v5.8.2/kustomize_v5.8.2_linux_amd64.tar.gz kustomize.tgz 06af0a202c2b831207d0173f9c9cdb1b30abceca0747cb3fbb72792d26055c95 kustomize
fetch https://github.com/yannh/kubeconform/releases/download/v0.8.0/kubeconform-linux-amd64.tar.gz kubeconform.tgz 9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883 kubeconform
if [[ -n ${GITHUB_PATH:-} ]]; then printf '%s\n' "$PWD" >> "$GITHUB_PATH"; fi
