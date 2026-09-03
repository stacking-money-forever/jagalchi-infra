#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/deploy/local.env}"
lock_file="$repo_root/deploy/local-stack.lock.json"
# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"

for command_name in docker curl node pnpm python3; do
  command -v "$command_name" >/dev/null || { echo "missing command: $command_name" >&2; exit 1; }
done
docker compose version >/dev/null

expected_node_major="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["nodeMajor"])' "$lock_file")"
expected_pnpm="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pnpm"])' "$lock_file")"
node_major="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$node_major" == "$expected_node_major" ]] || { echo "Node $expected_node_major is required; found $(node --version)" >&2; exit 1; }
[[ "$(pnpm --version)" == "$expected_pnpm" ]] || { echo "pnpm $expected_pnpm is required; found $(pnpm --version)" >&2; exit 1; }

if [[ ! -f "$env_file" ]]; then
  echo "missing local environment file: $env_file" >&2
  exit 1
fi

mode="$(stat -f '%Lp' "$env_file" 2>/dev/null || stat -c '%a' "$env_file")"
mode_value=$((8#$mode))
if (( (mode_value & 077) != 0 )); then
  echo "local environment file must not be group/world accessible: $env_file ($mode)" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

for variable_name in PLATFORM_SOURCE_DIR API_SOURCE_DIR AI_SOURCE_DIR JAGALCHI_LOCAL_MODE JOB_SOURCE_PROVIDER GITHUB_PROVIDER AI_PROVIDER AI_V1_PROVIDER API_POSTGRES_PASSWORD AI_POSTGRES_PASSWORD JWT_ACCESS_SECRET AI_AUTH_JWT_SECRET VERIFICATION_CODE_SECRET RATE_LIMIT_HASH_SECRET DJANGO_SECRET_KEY OBJECT_STORAGE_ACCESS_KEY_ID OBJECT_STORAGE_SECRET_ACCESS_KEY OBJECT_STORAGE_PRESIGN_ENDPOINT LOCAL_SEED_EMAIL LOCAL_SEED_PASSWORD; do
  [[ -n "${!variable_name:-}" ]] || { echo "missing environment key: $variable_name" >&2; exit 1; }
done

python3 "$repo_root/deploy/validate-local-lock.py" \
  --lock "$lock_file" \
  --repo-root "$repo_root" \
  --platform-source "$PLATFORM_SOURCE_DIR" \
  --api-source "$API_SOURCE_DIR" \
  --ai-source "$AI_SOURCE_DIR"

docker compose -p "$project_name" --env-file "$env_file" -f "$compose_file" config --quiet
echo "local doctor: OK"
