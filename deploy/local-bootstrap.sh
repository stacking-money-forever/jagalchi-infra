#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$repo_root/deploy/local.env}"
clone_mode="${2:-}"
lock_file="$repo_root/deploy/local-stack.lock.json"

command -v node >/dev/null || { echo "missing command: node" >&2; exit 1; }
command -v pnpm >/dev/null || { echo "missing command: pnpm" >&2; exit 1; }
command -v python3 >/dev/null || { echo "missing command: python3" >&2; exit 1; }
expected_node_major="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["nodeMajor"])' "$lock_file")"
expected_pnpm="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pnpm"])' "$lock_file")"
node_major="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$node_major" == "$expected_node_major" ]] || { echo "Node $expected_node_major is required; found $(node --version)" >&2; exit 1; }
[[ "$(pnpm --version)" == "$expected_pnpm" ]] || { echo "pnpm $expected_pnpm is required; found $(pnpm --version)" >&2; exit 1; }

if [[ ! -f "$env_file" ]]; then
  echo "missing local environment file: $env_file" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

clone_if_missing() {
  local destination="$1"
  local repository_url="$2"
  local source_key="$3"
  if [[ -e "$destination" ]]; then
    return
  fi
  if [[ "$clone_mode" != "--clone-missing" ]]; then
    echo "missing checkout: $destination (rerun with --clone-missing to clone it)" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$destination")"
  git clone --filter=blob:none "$repository_url" "$destination"
  local expected="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['revisions'][sys.argv[2]])" "$lock_file" "$source_key")"
  git -C "$destination" checkout --detach "$expected"
  git -C "$destination" submodule update --init --recursive 2>/dev/null || true
}

# 소스 디렉터리 키 매핑 (local.env의 변수명 -> lock revisions 키)
clone_if_missing "$PLATFORM_SOURCE_DIR" https://github.com/stacking-money-forever/jagalchi-platform.git platform
clone_if_missing "$API_SOURCE_DIR" https://github.com/stacking-money-forever/jagalchi-api.git api
clone_if_missing "$AI_SOURCE_DIR" https://github.com/stacking-money-forever/jagalchi-ai.git ai

"$repo_root/deploy/local-doctor.sh" "$env_file"

for source_dir in "$PLATFORM_SOURCE_DIR" "$API_SOURCE_DIR" "$AI_SOURCE_DIR"; do
  if [[ -n "$(git -C "$source_dir" status --porcelain)" ]]; then
    echo "refusing dependency bootstrap in dirty checkout: $source_dir" >&2
    exit 1
  fi
done
pnpm --dir "$PLATFORM_SOURCE_DIR" install --frozen-lockfile
pnpm --dir "$API_SOURCE_DIR" install --frozen-lockfile
python3 -m venv "$AI_SOURCE_DIR/.venv"
"$AI_SOURCE_DIR/.venv/bin/python" -m pip install --requirement "$AI_SOURCE_DIR/requirements.txt"
echo "local bootstrap: OK"
