#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/deploy/local.env}"
# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"
compose=(docker compose -p "$project_name" --env-file "$env_file" -f "$compose_file")

curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/health >/dev/null
ready_body="$(curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/health/ready)"
python3 -c 'import json,sys; body=json.loads(sys.argv[1]); assert body.get("status")=="ready"' "$ready_body"
"${compose[@]}" exec -T workflow-worker node dist/workflow/health-check.js >/dev/null
"${compose[@]}" exec -T ai curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/ai/health/ >/dev/null
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:9000/minio/health/live >/dev/null
echo "local smoke: OK"
