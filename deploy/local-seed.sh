#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/deploy/local.env}"
# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"
"$repo_root/deploy/local-doctor.sh" "$env_file" >/dev/null

compose=(docker compose -p "$project_name" --env-file "$env_file" -f "$compose_file")
seed_output="$("${compose[@]}" --profile tools run --rm --no-deps --build -T api-seed)"
seed_json="$(printf '%s\n' "$seed_output" | tail -n 1)"
python3 -c '
import json, sys
value = json.loads(sys.argv[1])
expected = {"schemaVersion", "userId", "projectRunId", "roadmapId"}
if set(value) != expected or value["schemaVersion"] != 1:
    raise SystemExit("invalid dev seed receipt")
for key in expected - {"schemaVersion"}:
    if not isinstance(value[key], str) or not value[key]:
        raise SystemExit(f"invalid dev seed receipt field: {key}")
print(json.dumps(value, separators=(",", ":")))
' "$seed_json"
