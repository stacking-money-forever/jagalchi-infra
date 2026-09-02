#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${DEPLOY_USER:-deploy}"

[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
id "$DEPLOY_USER" >/dev/null 2>&1 || { echo "deploy user not found: $DEPLOY_USER" >&2; exit 1; }

install -d -o root -g "$DEPLOY_USER" -m 750 /etc/jagalchi
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 750 /srv/jagalchi-cd
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 700 /srv/jagalchi-cd/home /srv/jagalchi-cd/config
install -d -o root -g root -m 755 /usr/local/libexec/jagalchi-backend-cd
install -o root -g root -m 755 "$ROOT_DIR/deploy/cd-poll.sh" /usr/local/libexec/jagalchi-backend-cd/cd-poll.sh
install -o root -g root -m 755 "$ROOT_DIR/deploy/verify-ci-run.py" /usr/local/libexec/jagalchi-backend-cd/verify-ci-run.py

if [[ ! -f /etc/jagalchi/backend-cd.env ]]; then
  install -o root -g "$DEPLOY_USER" -m 640 "$ROOT_DIR/deploy/backend-cd.env.example" /etc/jagalchi/backend-cd.env
fi
install -o root -g root -m 644 "$ROOT_DIR/deploy/systemd/jagalchi-backend-cd.service" /etc/systemd/system/jagalchi-backend-cd.service
install -o root -g root -m 644 "$ROOT_DIR/deploy/systemd/jagalchi-backend-cd.timer" /etc/systemd/system/jagalchi-backend-cd.timer

systemctl daemon-reload
systemctl enable --now jagalchi-backend-cd.timer
echo "backend CD timer installed; CD_ENABLED remains controlled by /etc/jagalchi/backend-cd.env"
