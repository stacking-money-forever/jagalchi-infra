#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="${1:-}"
reset="${2:-}"
[[ -n "$env_file" && ( -z "$reset" || "$reset" == "--reset" ) && $# -le 2 ]] || {
  echo "usage: $0 /absolute/path/to/local.env [--reset]" >&2
  exit 2
}
[[ "$env_file" == /* && -f "$env_file" ]] || { echo "local acceptance requires an absolute env file" >&2; exit 2; }

# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"
"$repo_root/deploy/local-doctor.sh" "$env_file"

if [[ "$reset" == "--reset" ]]; then
  "$repo_root/deploy/local-reset.sh" "$env_file" "--confirm=$project_name"
fi

"$repo_root/deploy/local-up.sh" "$env_file"
seed_receipt="$("$repo_root/deploy/local-seed.sh" "$env_file")"
receipt_arguments=()
if [[ "$reset" == "--reset" ]]; then
  receipt_arguments+=(--reset-performed)
fi
python3 "$repo_root/deploy/local_acceptance.py" \
  --env "$env_file" \
  --repo-root "$repo_root" \
  --seed-receipt "$seed_receipt" \
  "${receipt_arguments[@]}"
