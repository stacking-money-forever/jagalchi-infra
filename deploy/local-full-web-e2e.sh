#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file=""
reset=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset)
      reset="--reset"
      shift
      ;;
    *)
      if [[ -z "$env_file" ]]; then
        env_file="$1"
      elif [[ -z "$reset" && "$1" == "--reset" ]]; then
        reset="--reset"
      else
        echo "usage: $0 /absolute/path/to/local.env [--reset]" >&2
        exit 2
      fi
      shift
      ;;
  esac
done

[[ -n "$env_file" && "$env_file" == /* && -f "$env_file" ]] || {
  echo "usage: $0 /absolute/path/to/local.env [--reset]" >&2
  exit 2
}

# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"

allow_dev_head_flag=()
if [[ "${JAGALCHI_DEV_HEAD:-}" == "true" ]]; then
  allow_dev_head_flag=(--allow-dev-head)
fi

"$repo_root/deploy/local-doctor.sh" "$env_file"

if [[ "$reset" == "--reset" ]]; then
  "$repo_root/deploy/local-reset.sh" "$env_file" "--confirm=$project_name"
fi

"$repo_root/deploy/local-up.sh" "$env_file"

seed_receipt="$("$repo_root/deploy/local-seed.sh" "$env_file")"

exec python3 "$repo_root/deploy/local_browser_gate.py" run-full-web-e2e \
  --env "$env_file" \
  --repo-root "$repo_root" \
  --seed-receipt "$seed_receipt" \
  "${allow_dev_head_flag[@]}"
