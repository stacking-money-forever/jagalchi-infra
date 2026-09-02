#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this script as root" >&2
  exit 1
fi

DEPLOY_USER="${1:-${SUDO_USER:-}}"
if [[ -n "$DEPLOY_USER" ]] && ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  echo "deploy user does not exist: $DEPLOY_USER" >&2
  exit 1
fi

. /etc/os-release
if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" ]]; then
  echo "this bootstrap supports Ubuntu and Debian only" >&2
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl git rsync
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
  "$(dpkg --print-architecture)" "$ID" "$VERSION_CODENAME" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

if [[ -n "$DEPLOY_USER" && "$DEPLOY_USER" != "root" ]]; then
  usermod -aG docker "$DEPLOY_USER"
  echo "Added $DEPLOY_USER to the docker group; log out and back in before deploying."
fi

echo "Docker is ready. Open inbound TCP 22, 80, and 443 in the host firewall/security group."
