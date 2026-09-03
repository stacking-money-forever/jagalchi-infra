#!/usr/bin/env bash

load_local_stack_lock() {
  local repo_root="$1"
  local lock_file="$repo_root/deploy/local-stack.lock.json"

  command -v python3 >/dev/null || { echo "missing command: python3" >&2; return 1; }
  project_name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project"])' "$lock_file")"
  compose_file="$repo_root/$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["composeFile"])' "$lock_file")"

  [[ "$project_name" == "jagalchi-v1-local" ]] || {
    echo "unsafe local Compose project in lock: $project_name" >&2
    return 1
  }
  [[ -f "$compose_file" ]] || { echo "missing locked Compose file: $compose_file" >&2; return 1; }
}
