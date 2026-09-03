#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/deploy/local.env}"
# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"
"$repo_root/deploy/local-doctor.sh" "$env_file"
docker compose -p "$project_name" --env-file "$env_file" -f "$compose_file" up --build -d --wait
"$repo_root/deploy/local-smoke.sh" "$env_file"
