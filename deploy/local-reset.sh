#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/deploy/local.env}"
confirmation="${2:-}"
# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"
if [[ "$confirmation" != "--confirm=$project_name" ]]; then
  echo "refusing destructive reset; pass --confirm=jagalchi-v1-local as the second argument" >&2
  exit 2
fi
docker compose -p "$project_name" --env-file "$env_file" -f "$compose_file" down --volumes --remove-orphans
echo "removed only jagalchi-v1-local containers and named project volumes"
