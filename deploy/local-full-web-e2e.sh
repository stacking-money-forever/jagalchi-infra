#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="${1:-}"

[[ -n "$env_file" && "$env_file" == /* && -f "$env_file" ]] || {
  echo "usage: $0 /absolute/path/to/local.env" >&2
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
"$repo_root/deploy/local-up.sh" "$env_file"

seed_receipt="$("$repo_root/deploy/local-seed.sh" "$env_file")"

python3 "$repo_root/deploy/local_browser_gate.py" validate \
  --env "$env_file" \
  --repo-root "$repo_root" \
  --seed-receipt "$seed_receipt" \
  --profile full-web \
  "${allow_dev_head_flag[@]}"

exec python3 "$repo_root/deploy/local_browser_gate.py" run-integrated \
  --env "$env_file" \
  --repo-root "$repo_root" \
  --seed-receipt "$seed_receipt" \
  --profile full-web \
  "${allow_dev_head_flag[@]}"
