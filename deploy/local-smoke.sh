#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/deploy/local.env}"
# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"
compose=(docker compose -p "$project_name" --env-file "$env_file" -f "$compose_file")

retry_curl() {
  local url="$1"
  local deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  curl --fail --silent --show-error --max-time 5 "$url" >/dev/null
}

retry_compose_exec() {
  local deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    if "${compose[@]}" exec -T "$@"; then
      return 0
    fi
    sleep 1
  done
  "${compose[@]}" exec -T "$@"
}

retry_curl http://127.0.0.1:8080/api/health
ready_body=""
ready_deadline=$((SECONDS + 60))
while (( SECONDS < ready_deadline )); do
  if ready_body="$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/health/ready 2>/dev/null)"; then
    break
  fi
  sleep 1
done
[[ -n "$ready_body" ]] || ready_body="$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/health/ready)"
python3 -c 'import json,sys; body=json.loads(sys.argv[1]); assert body.get("status")=="ready"' "$ready_body"
retry_compose_exec workflow-worker node dist/workflow/health-check.js
retry_compose_exec ai curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/ai/health/
retry_curl http://127.0.0.1:9000/minio/health/live
echo "local smoke: OK"
