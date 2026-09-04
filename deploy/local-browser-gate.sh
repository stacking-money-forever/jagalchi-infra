#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="${1:-}"
seed_receipt="${JAGALCHI_SEED_RECEIPT:-{}}"

[[ -n "$env_file" && "$env_file" == /* && -f "$env_file" ]] || {
  echo "usage: $0 /absolute/path/to/local.env" >&2
  exit 2
}

# shellcheck disable=SC1091
source "$repo_root/deploy/local-common.sh"
load_local_stack_lock "$repo_root"

python3 "$repo_root/deploy/local_browser_gate.py" validate \
  --env "$env_file" \
  --repo-root "$repo_root" \
  --seed-receipt "$seed_receipt"

platform_source="$(python3 - <<'PY' "$env_file"
import sys
from pathlib import Path

env_file = Path(sys.argv[1])
values = {}
for raw_line in env_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip("'\"")
platform = values.get("PLATFORM_SOURCE_DIR", "")
if not platform:
    raise SystemExit("PLATFORM_SOURCE_DIR is missing from env file")
print(platform)
PY
)"

test_script="$platform_source/scripts/test-v1-local-e2e.sh"
[[ -x "$test_script" || -f "$test_script" ]] || {
  echo "platform no-MSW harness script is missing" >&2
  exit 1
}

exec "$test_script" "$repo_root" "$env_file"
