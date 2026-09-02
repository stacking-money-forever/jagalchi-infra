#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "usage: $0 /absolute/path/to/jagalchi-production.env" >&2
  exit 2
fi

env_value() {
  awk -F= -v wanted="$1" '$1 == wanted {sub(/^[^=]*=/, ""); gsub(/^['"'"']|['"'"']$/, ""); print; exit}' "$ENV_FILE"
}

API_DOMAIN="$(env_value API_DOMAIN)"
UPLOADS_DOMAIN="$(env_value UPLOADS_DOMAIN)"
WEB_APP_URL="$(env_value WEB_APP_URL)"
[[ -n "$API_DOMAIN" && -n "$UPLOADS_DOMAIN" && -n "$WEB_APP_URL" ]]

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$ROOT_DIR/compose.production.yml" --profile cloudflare-tunnel)
deadline=$((SECONDS + 300))

while ((SECONDS < deadline)); do
  if curl --fail --silent --show-error --max-time 10 "https://${API_DOMAIN}/api/health/ready" >/dev/null \
    && curl --fail --silent --show-error --max-time 10 "https://${UPLOADS_DOMAIN}/minio/health/live" >/dev/null; then
    healthy=0
    for service in api ai ai-db minio cloudflared; do
      id="$("${COMPOSE[@]}" ps -q "$service")"
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || true)"
      [[ "$status" == "healthy" || "$status" == "running" ]] || healthy=1
    done
    ((healthy == 0)) && break
  fi
  sleep 5
done

if ((SECONDS >= deadline)); then
  echo "production did not become healthy within 5 minutes" >&2
  "${COMPOSE[@]}" ps >&2
  "${COMPOSE[@]}" logs --tail=100 api ai cloudflared >&2
  exit 1
fi

"${COMPOSE[@]}" exec -T ai python -m jagalchi_ai.ai_core.controller.verify_providers >/dev/null

"${COMPOSE[@]}" exec -T api node -e '
  fetch("https://api.resend.com/domains", {
    headers: {Authorization: `Bearer ${process.env.RESEND_API_KEY}`},
  }).then((response) => {
    if (!response.ok) process.exit(1);
  }).catch(() => process.exit(1));
'

for provider in google github; do
  redirect="$(curl --silent --show-error --max-time 10 --output /dev/null --write-out '%{redirect_url}' \
    "https://${API_DOMAIN}/api/users/auth/login/${provider}?returnUrl=${WEB_APP_URL}")"
  case "$provider:$redirect" in
    google:https://accounts.google.com/*|github:https://github.com/*) ;;
    *) echo "$provider OAuth redirect smoke failed" >&2; exit 1 ;;
  esac
done

webhook_status="$(curl --silent --show-error --max-time 10 --output /dev/null --write-out '%{http_code}' \
  -X POST -H 'content-type: application/json' -H 'x-github-event: pull_request' \
  -H 'x-github-delivery: 00000000-0000-4000-8000-000000000000' \
  -H 'x-hub-signature-256: sha256=invalid' --data '{}' \
  "https://${API_DOMAIN}/api/github/webhooks")"
[[ "$webhook_status" == "401" || "$webhook_status" == "403" ]] || {
  echo "GitHub App webhook boundary smoke failed with HTTP $webhook_status" >&2
  exit 1
}

echo "production smoke check passed"
