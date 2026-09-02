#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${CD_CONFIG_FILE:-/etc/jagalchi/backend-cd.env}"
DRY_RUN=false
RETRY_FAILED=false

for argument in "$@"; do
  case "$argument" in
    --dry-run) DRY_RUN=true ;;
    --retry-failed) RETRY_FAILED=true ;;
    *) echo "unknown argument: $argument" >&2; exit 2 ;;
  esac
done

[[ -f "$CONFIG_FILE" ]] || { echo "CD config not found: $CONFIG_FILE" >&2; exit 1; }
# The installer owns this root-controlled, non-secret shell environment file.
# shellcheck disable=SC1090
. "$CONFIG_FILE"

: "${CD_ENABLED:=false}"
: "${CD_REPOSITORY:=stacking-money-forever/jagalchi-infra}"
: "${CD_BRANCH:=main}"
: "${CD_REQUIRED_WORKFLOW:=CI}"
: "${CD_REQUIRED_WORKFLOW_PATH:=.github/workflows/ci.yml}"
: "${CD_ROOT:=/srv/jagalchi-cd}"
: "${CD_ENV_FILE:=/etc/jagalchi/jagalchi-production.env}"
: "${CD_RELEASE_RETENTION:=5}"

[[ "$CD_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "invalid CD_REPOSITORY" >&2; exit 1; }
[[ "$CD_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "invalid CD_BRANCH" >&2; exit 1; }
[[ "$CD_REQUIRED_WORKFLOW_PATH" =~ ^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$ ]] || { echo "invalid CD_REQUIRED_WORKFLOW_PATH" >&2; exit 1; }
[[ "$CD_ROOT" == /srv/* && "$CD_ROOT" != "/srv" ]] || { echo "CD_ROOT must be a child of /srv" >&2; exit 1; }
[[ -f "$CD_ENV_FILE" ]] || { echo "production env not found: $CD_ENV_FILE" >&2; exit 1; }
[[ "$CD_RELEASE_RETENTION" =~ ^[1-9][0-9]*$ ]] || { echo "CD_RELEASE_RETENTION must be positive" >&2; exit 1; }

mkdir -p "$CD_ROOT/state" "$CD_ROOT/releases" "$CD_ROOT/tmp"
exec 9>"$CD_ROOT/deploy.lock"
if ! flock -n 9; then
  echo "another backend CD check is active"
  exit 0
fi

remote_url="https://github.com/${CD_REPOSITORY}.git"
target_sha="$(git ls-remote --exit-code --refs "$remote_url" "refs/heads/$CD_BRANCH" | awk 'NR == 1 {print $1}')"
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "could not resolve target commit" >&2; exit 1; }

if [[ -f "$CD_ROOT/state/deployed-sha" ]] && [[ "$(<"$CD_ROOT/state/deployed-sha")" == "$target_sha" ]]; then
  echo "backend already deployed: ${target_sha:0:12}"
  exit 0
fi
if [[ "$RETRY_FAILED" != "true" && -f "$CD_ROOT/state/failed-sha" ]] \
  && [[ "$(<"$CD_ROOT/state/failed-sha")" == "$target_sha" ]]; then
  echo "backend deployment previously failed for ${target_sha:0:12}; use --retry-failed after remediation"
  exit 0
fi

workflow_payload="$CD_ROOT/tmp/workflow-runs.json"
curl --fail --silent --show-error --location --max-time 30 \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2026-03-10" \
  "https://api.github.com/repos/${CD_REPOSITORY}/actions/runs?branch=${CD_BRANCH}&event=push&head_sha=${target_sha}&per_page=20" \
  > "$workflow_payload"

set +e
ci_verdict="$(python3 "$SCRIPT_DIR/verify-ci-run.py" "$workflow_payload" "$CD_REQUIRED_WORKFLOW" "$CD_REQUIRED_WORKFLOW_PATH" "$target_sha")"
ci_status=$?
set -e
case "$ci_status:$ci_verdict" in
  0:success) ;;
  75:pending) echo "CI is not complete for ${target_sha:0:12}"; exit 0 ;;
  1:failure)
    echo "CI failed for ${target_sha:0:12}; deployment refused" >&2
    exit 1
    ;;
  *) echo "could not classify CI for ${target_sha:0:12}" >&2; exit 1 ;;
esac

mirror="$CD_ROOT/repository.git"
if [[ ! -d "$mirror" ]]; then
  git clone --mirror --filter=blob:none "$remote_url" "$mirror"
else
  git --git-dir="$mirror" fetch --prune --filter=blob:none origin "+refs/heads/$CD_BRANCH:refs/heads/$CD_BRANCH"
fi
git --git-dir="$mirror" cat-file -e "${target_sha}^{commit}"

release_dir="$CD_ROOT/releases/$target_sha"
if [[ ! -d "$release_dir" ]]; then
  git --git-dir="$mirror" worktree add --detach "$release_dir" "$target_sha"
fi
[[ "$(git -C "$release_dir" rev-parse HEAD)" == "$target_sha" ]]
[[ -z "$(git -C "$release_dir" status --porcelain)" ]] || { echo "release worktree is dirty" >&2; exit 1; }

prune_releases() {
  local deployed_sha="" index=0 line release_path release_sha
  [[ -f "$CD_ROOT/state/deployed-sha" ]] && deployed_sha="$(<"$CD_ROOT/state/deployed-sha")"
  while IFS= read -r line; do
    release_path="${line#* }"
    release_sha="${release_path##*/}"
    [[ "$release_path" == "$CD_ROOT/releases/"* && "$release_sha" =~ ^[0-9a-f]{40}$ ]] || continue
    index=$((index + 1))
    if ((index <= CD_RELEASE_RETENTION)) || [[ "$release_sha" == "$target_sha" || "$release_sha" == "$deployed_sha" ]]; then
      continue
    fi
    git --git-dir="$mirror" worktree remove --force "$release_path"
  done < <(find "$CD_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr)
  git --git-dir="$mirror" worktree prune
}

if [[ "$DRY_RUN" == "true" ]]; then
  echo "CD dry-run ready: ${target_sha:0:12} after successful CI"
  exit 0
fi
prune_releases
if [[ "$CD_ENABLED" != "true" ]]; then
  echo "CD is staged but disabled for ${target_sha:0:12}"
  exit 0
fi

confirmed_sha="$(git ls-remote --exit-code --refs "$remote_url" "refs/heads/$CD_BRANCH" | awk 'NR == 1 {print $1}')"
if [[ "$confirmed_sha" != "$target_sha" ]]; then
  echo "main advanced during verification; deferring stale release ${target_sha:0:12}"
  exit 0
fi

printf '%s\n' "$target_sha" > "$CD_ROOT/state/attempted-sha"
if DEPLOY_STATE_DIR="$CD_ROOT/state/deploy" RELEASE="${target_sha:0:12}" \
  "$release_dir/deploy/deploy.sh" "$CD_ENV_FILE"; then
  printf '%s\n' "$target_sha" > "$CD_ROOT/state/deployed-sha"
  : > "$CD_ROOT/state/failed-sha"
  ln -sfn "$release_dir" "$CD_ROOT/current.next"
  mv -Tf "$CD_ROOT/current.next" "$CD_ROOT/current"
  echo "backend CD completed: ${target_sha:0:12}"
else
  printf '%s\n' "$target_sha" > "$CD_ROOT/state/failed-sha"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$CD_ROOT/state/failed-at"
  echo "backend CD failed: ${target_sha:0:12}" >&2
  exit 1
fi
