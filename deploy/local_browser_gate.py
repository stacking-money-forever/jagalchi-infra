#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any


class BrowserGateError(RuntimeError):
    pass


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
SECRET_ENV_KEYS = (
    "LOCAL_SEED_PASSWORD",
    "DEEPSEEK_API_KEY",
    "JWT_ACCESS_SECRET",
    "AI_AUTH_JWT_SECRET",
    "VERIFICATION_CODE_SECRET",
    "RATE_LIMIT_HASH_SECRET",
    "DJANGO_SECRET_KEY",
    "OBJECT_STORAGE_ACCESS_KEY_ID",
    "OBJECT_STORAGE_SECRET_ACCESS_KEY",
    "POSTGRES_PASSWORD",
)


@dataclass(frozen=True)
class BrowserGatePlan:
    platform_source: Path
    platform_revision: str
    env_file: Path
    infra_root: Path
    seed: dict[str, Any]
    playwright_env: dict[str, str]
    standalone_command: list[str]
    integrated_build_command: list[str]
    integrated_playwright_command: list[str]
    playwright_grep: str | None
    profile: str


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def git_head(source_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, OSError) as error:
        raise BrowserGateError(f"cannot read git HEAD from {source_dir}") from error
    return result.stdout.strip()


def load_manifest(repo_root: Path) -> dict[str, Any]:
    manifest_path = repo_root / "deploy/e2e-v1-local.manifest.json"
    if not manifest_path.is_file():
        raise BrowserGateError("browser gate manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != 1:
        raise BrowserGateError("unsupported browser gate manifest schema")
    return manifest


WEB_DIR_PREFIX = "apps/web/"
E2E_PREFIX = "e2e-v1-local/"
ROLLBACK_PROFILE = "rollback"
PROJECT_RUNS_ROLLBACK_PROFILE = "project-runs-rollback"
ROLLBACK_PROFILES = frozenset({ROLLBACK_PROFILE, PROJECT_RUNS_ROLLBACK_PROFILE})
SUPPORTED_PROFILES = frozenset({"phase1", "phase2", "full-web", *ROLLBACK_PROFILES})
PROJECT_RUNS_PROFILES = frozenset({"phase2", "full-web"})
FULL_WEB_PROFILE = "full-web"
PHASE2_CLOSURE_SPEC = "e2e-v1-local/phase-two-closure.spec.ts"
PHASE2_CLOSURE_MARKER_PREFIX = "phase2-closure:"
PHASE2_ROLLBACK_SPEC = "e2e-v1-local/phase-two-rollback.spec.ts"
PHASE2_ROLLBACK_MARKER_PREFIX = "phase2-rollback:"
PLAYWRIGHT_ARTIFACT_DIRS = ("test-results", "e2e-v1-local/.auth")




def resolve_platform_inventory_path(platform_source: Path, relative: str) -> Path:
    if relative.startswith(E2E_PREFIX):
        return platform_source / WEB_DIR_PREFIX / relative
    return platform_source / relative


def playwright_spec_argument(spec_path: str) -> str:
    if spec_path.startswith(WEB_DIR_PREFIX):
        return spec_path[len(WEB_DIR_PREFIX) :]
    if spec_path.startswith(E2E_PREFIX):
        return spec_path
    raise BrowserGateError("browser spec path must be web-relative")


def validate_spec_list(
    platform_source: Path,
    specs: object,
    *,
    manifest_key: str,
) -> list[str]:
    if not isinstance(specs, list) or not specs:
        raise BrowserGateError(f"browser gate manifest {manifest_key} is invalid")
    invalid = [
        relative
        for relative in specs
        if not isinstance(relative, str) or relative.startswith(WEB_DIR_PREFIX)
    ]
    if invalid:
        raise BrowserGateError(f"{manifest_key} browser spec paths must be web-relative")
    missing = [
        relative
        for relative in specs
        if not resolve_platform_inventory_path(platform_source, relative).is_file()
    ]
    if missing:
        raise BrowserGateError(f"{manifest_key} browser spec is missing from platform checkout")
    return [str(relative) for relative in specs]
def phase2_closure_spec(manifest: dict[str, Any]) -> tuple[str, list[str]]:
    contract = manifest.get("phase2ClosureSpec")
    if not isinstance(contract, dict):
        raise BrowserGateError("browser gate phase2ClosureSpec is invalid")
    spec = contract.get("path")
    if spec != PHASE2_CLOSURE_SPEC:
        raise BrowserGateError(
            f"phase2 closure spec must be {PHASE2_CLOSURE_SPEC}"
        )
    markers = contract.get("requiredMarkers")
    if not isinstance(markers, list) or not markers:
        raise BrowserGateError("browser gate phase2 closure markers are invalid")
    if any(
        not isinstance(marker, str)
        or not marker.startswith(PHASE2_CLOSURE_MARKER_PREFIX)
        for marker in markers
    ):
        raise BrowserGateError("browser gate phase2 closure markers are malformed")
    if len(set(markers)) != len(markers):
        raise BrowserGateError("browser gate phase2 closure markers are duplicated")
    return spec, [str(marker) for marker in markers]


def validate_phase2_closure(
    platform_source: Path,
    manifest: dict[str, Any],
) -> list[str]:
    spec, markers = phase2_closure_spec(manifest)
    spec_path = resolve_platform_inventory_path(platform_source, spec)
    if not spec_path.is_file():
        raise BrowserGateError("phase2 closure spec is missing from platform checkout")
    source = spec_path.read_text(encoding="utf-8")
    missing = [marker for marker in markers if marker not in source]
    if missing:
        raise BrowserGateError(
            "phase2 closure spec is missing required markers: "
            + ", ".join(missing)
        )
    return [spec]


def validate_phase2_rollback(
    platform_source: Path,
    manifest: dict[str, Any],
) -> list[str]:
    contract = manifest.get("rollbackSpec")
    if not isinstance(contract, dict):
        raise BrowserGateError("browser gate rollbackSpec is invalid")
    spec = contract.get("path")
    if spec != PHASE2_ROLLBACK_SPEC:
        raise BrowserGateError(f"rollback spec must be {PHASE2_ROLLBACK_SPEC}")
    markers = contract.get("requiredMarkers")
    if not isinstance(markers, list) or not markers:
        raise BrowserGateError("browser gate rollback markers are invalid")
    if any(
        not isinstance(marker, str)
        or not marker.startswith(PHASE2_ROLLBACK_MARKER_PREFIX)
        for marker in markers
    ):
        raise BrowserGateError("browser gate rollback markers are malformed")
    if len(set(markers)) != len(markers):
        raise BrowserGateError("browser gate rollback markers are duplicated")
    spec_path = resolve_platform_inventory_path(platform_source, spec)
    if not spec_path.is_file():
        raise BrowserGateError("rollback spec is missing from platform checkout")
    source = spec_path.read_text(encoding="utf-8")
    missing = [marker for marker in markers if marker not in source]
    if missing:
        raise BrowserGateError(
            "rollback spec is missing required markers: " + ", ".join(missing)
        )
    return [spec]


def append_unique(items: list[str], additions: list[str]) -> list[str]:
    result = list(items)
    for item in additions:
        if item not in result:
            result.append(item)
    return result


def profile_specs(
    platform_source: Path,
    manifest: dict[str, Any],
    profile: str,
) -> list[str]:
    if profile in ROLLBACK_PROFILES:
        return validate_phase2_rollback(platform_source, manifest)
    if profile == "phase2":
        base = validate_spec_list(
            platform_source,
            manifest.get("phase2RequiredSpecs"),
            manifest_key="phase2RequiredSpecs",
        )
        return append_unique(base, validate_phase2_closure(platform_source, manifest))
    if profile == FULL_WEB_PROFILE:
        base = validate_spec_list(
            platform_source,
            manifest.get("v1LocalRequiredSpecs"),
            manifest_key="v1LocalRequiredSpecs",
        )
        return append_unique(base, validate_phase2_closure(platform_source, manifest))
    return []




def manifest_specs_for_profile(
    platform_source: Path,
    manifest: dict[str, Any],
    profile: str,
) -> list[str]:
    return profile_specs(platform_source, manifest, profile)




def validate_inventory(
    platform_source: Path,
    manifest: dict[str, Any],
    *,
    profile: str = "phase1",
) -> None:
    phase2_specs = manifest.get("phase2RequiredSpecs")
    v1_specs = manifest.get("v1LocalRequiredSpecs")
    if profile not in {"phase2", FULL_WEB_PROFILE, *ROLLBACK_PROFILES}:
        if isinstance(phase2_specs, list):
            phase2_specs = [spec for spec in phase2_specs if spec != PHASE2_CLOSURE_SPEC]
        if isinstance(v1_specs, list):
            v1_specs = [spec for spec in v1_specs if spec != PHASE2_CLOSURE_SPEC]
    validate_spec_list(
        platform_source,
        phase2_specs,
        manifest_key="phase2RequiredSpecs",
    )
    validate_spec_list(
        platform_source,
        v1_specs,
        manifest_key="v1LocalRequiredSpecs",
    )
    if profile in {"phase2", FULL_WEB_PROFILE}:
        validate_phase2_closure(platform_source, manifest)
    elif profile in ROLLBACK_PROFILES:
        validate_phase2_rollback(platform_source, manifest)

    required_files = manifest.get("requiredFiles")
    if not isinstance(required_files, list) or not required_files:
        raise BrowserGateError("browser gate manifest requiredFiles is invalid")
    missing = [
        relative
        for relative in required_files
        if not isinstance(relative, str)
        or not resolve_platform_inventory_path(platform_source, relative).is_file()
    ]
    if missing:
        raise BrowserGateError("browser gate platform inventory is incomplete")

def validate_platform_revision(
    platform_source: Path,
    lock: dict[str, Any],
    *,
    allow_dev_head: bool,
) -> str:
    actual = git_head(platform_source)
    expected = lock.get("revisions", {}).get("platform")
    if not isinstance(expected, str) or len(expected) != 40:
        raise BrowserGateError("locked platform revision is missing")
    if not allow_dev_head and actual != expected:
        raise BrowserGateError(
            "unpinned platform checkout for browser gate "
            f"(expected {expected}, found {actual}; set JAGALCHI_DEV_HEAD=true to override)"
        )
    return actual
def validate_no_msw_contract(platform_source: Path, *, profile: str) -> None:
    config_path = platform_source / "apps/web/playwright.v1-local.config.ts"
    helper_path = platform_source / "apps/web/e2e-v1-local/helpers.ts"
    config = config_path.read_text(encoding="utf-8")
    helper = helper_path.read_text(encoding="utf-8")
    if not re.search(r"serviceWorkers\s*:\s*['\"]block['\"]", config):
        raise BrowserGateError("Playwright no-MSW service-worker blocking is missing")
    for marker in ("NEXT_PUBLIC_API_MOCKING", "NEXT_PUBLIC_E2E_MOCKING"):
        if marker not in config:
            raise BrowserGateError(f"Playwright no-MSW flag contract is missing: {marker}")
    if profile in ROLLBACK_PROFILES:
        for marker in (
            "NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED",
            "NEXT_PUBLIC_PROJECT_RUNS_ENABLED",
        ):
            if re.search(rf"{marker}\s*:\s*['\"](?:true|false)['\"]", config):
                raise BrowserGateError(
                    f"rollback requires Playwright webServer to honor injected {marker}"
                )
    if "expectNoServiceWorker" not in helper or "navigator.serviceWorker" not in helper:
        raise BrowserGateError("browser no-service-worker assertion is missing")




def require_seed_uuid(seed: dict[str, Any], key: str) -> str:
    value = seed.get(key)
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise BrowserGateError(f"seed {key} is not a backend UUID")
    return value


def browser_gate_env(
    env: dict[str, str],
    seed: dict[str, Any],
    *,
    profile: str = "phase1",
) -> dict[str, str]:
    user_id = require_seed_uuid(seed, "userId")
    project_run_id = require_seed_uuid(seed, "projectRunId")
    roadmap_id = require_seed_uuid(seed, "roadmapId")
    email = env.get("LOCAL_SEED_EMAIL", "")
    password = env.get("LOCAL_SEED_PASSWORD", "")
    if not email or not password:
        raise BrowserGateError("LOCAL_SEED_EMAIL and LOCAL_SEED_PASSWORD are required")
    web_port = env.get("E2E_WEB_PORT", "3100")
    if not web_port.isdigit() or not 1024 <= int(web_port) <= 65535:
        raise BrowserGateError("E2E_WEB_PORT must be an integer between 1024 and 65535")
    base_url = f"http://127.0.0.1:{web_port}"
    playwright_env = {
        "E2E_TEST_EMAIL": email,
        "E2E_TEST_PASSWORD": password,
        "E2E_SEED_USER_ID": user_id,
        "E2E_SEED_PROJECT_RUN_ID": project_run_id,
        "E2E_SEED_ROADMAP_ID": roadmap_id,
        "E2E_WEB_PORT": web_port,
        "E2E_BASE_URL": base_url,
        "JAGALCHI_E2E_SEED_RUN_ID": project_run_id,
        "API_ORIGIN": "http://127.0.0.1:8080",
        "NEXT_PUBLIC_API_URL": "/api",
        "NEXT_PUBLIC_ENV": "development",
        "NEXT_PUBLIC_ANALYTICS_ENABLED": "false",
        "NEXT_PUBLIC_API_MOCKING": "false",
        "NEXT_PUBLIC_E2E_MOCKING": "false",
        "NEXT_PUBLIC_REALTIME_ENABLED": "true",
        "NEXT_PUBLIC_REALTIME_URL": "http://127.0.0.1:8080",
        "NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED": "false"
        if profile == ROLLBACK_PROFILE
        else "true",
        "NEXT_PUBLIC_PROOF_PROFILE_ENABLED": "true",
        "NEXT_PUBLIC_SITE_URL": base_url,
        # Force a new production build and prevent Playwright web-server reuse.
        "CI": "true",
    }
    if profile in PROJECT_RUNS_PROFILES:
        # Wave B entry routes compile to notFound() unless both flags are baked into `pnpm build`.
        playwright_env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"] = "true"
    elif profile in ROLLBACK_PROFILES:
        playwright_env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"] = "false"
    return playwright_env


def redact_output(text: str, env: dict[str, str]) -> str:
    redacted = text
    for key in SECRET_ENV_KEYS:
        value = env.get(key)
        if value and len(value) >= 4:
            redacted = redacted.replace(value, "[REDACTED]")
    for match in re.findall(r"Bearer\s+[A-Za-z0-9._-]+", redacted):
        redacted = redacted.replace(match, "Bearer [REDACTED]")
    return redacted


def build_plan(
    *,
    repo_root: Path,
    env_file: Path,
    seed: dict[str, Any],
    allow_dev_head: bool | None = None,
    require_seed: bool = True,
    profile: str = "phase1",
) -> BrowserGatePlan:
    env = read_env(env_file)
    platform_source = Path(env.get("PLATFORM_SOURCE_DIR", ""))
    if not platform_source.is_dir():
        raise BrowserGateError("PLATFORM_SOURCE_DIR is missing")
    platform_source = platform_source.resolve()
    manifest = load_manifest(repo_root)
    validate_inventory(platform_source, manifest, profile=profile)
    lock = json.loads((repo_root / "deploy/local-stack.lock.json").read_text(encoding="utf-8"))
    if allow_dev_head is None:
        allow_dev_head = os.environ.get("JAGALCHI_DEV_HEAD", "") == "true"
    if profile not in SUPPORTED_PROFILES:
        raise BrowserGateError(f"unsupported browser gate profile: {profile}")
    if profile in {"phase2", FULL_WEB_PROFILE, *ROLLBACK_PROFILES}:
        validate_no_msw_contract(platform_source, profile=profile)
    platform_revision = validate_platform_revision(platform_source, lock, allow_dev_head=allow_dev_head)
    playwright_env = browser_gate_env(env, seed, profile=profile) if require_seed else {}
    test_script = platform_source / "scripts/test-v1-local-e2e.sh"
    if not test_script.is_file():
        raise BrowserGateError("platform no-MSW harness script is missing")
    web_dir = platform_source / "apps/web"
    integrated_playwright_command = [
        "pnpm",
        "--dir",
        str(web_dir),
        "exec",
        "playwright",
        "test",
        "--config",
        "playwright.v1-local.config.ts",
    ]
    playwright_grep = None
    for spec in manifest_specs_for_profile(platform_source, manifest, profile):
        integrated_playwright_command.append(playwright_spec_argument(spec))
    return BrowserGatePlan(
        platform_source=platform_source,
        platform_revision=platform_revision,
        env_file=env_file,
        infra_root=repo_root,
        seed=seed,
        playwright_env=playwright_env,
        standalone_command=[str(test_script), str(repo_root), str(env_file)],
        integrated_build_command=["pnpm", "--dir", str(web_dir), "build"],
        integrated_playwright_command=integrated_playwright_command,
        playwright_grep=playwright_grep,
        profile=profile,
    )


def run_command(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )


PLAYWRIGHT_SPEC_OFFSET = 8


def playwright_specs(command: list[str]) -> list[str]:
    return command[PLAYWRIGHT_SPEC_OFFSET:]


def playwright_command_for_specs(command: list[str], specs: list[str]) -> list[str]:
    return command[:PLAYWRIGHT_SPEC_OFFSET] + specs


def integrated_playwright_commands(
    plan: BrowserGatePlan,
    *,
    between_spec_runs: Callable[[], None] | None = None,
) -> list[list[str]]:
    base = plan.integrated_playwright_command
    specs = playwright_specs(base)
    if not specs:
        return [base]
    if between_spec_runs is not None and len(specs) > 1 and plan.profile == "phase2":
        return [playwright_command_for_specs(base, [spec]) for spec in specs]
    return [base]


def run_integrated(
    plan: BrowserGatePlan,
    env: dict[str, str],
    *,
    between_spec_runs: Callable[[], None] | None = None,
) -> str:
    build = run_command(plan.integrated_build_command, env=plan.playwright_env)
    synthetic_canary = plan.playwright_env.get("JAGALCHI_E2E_SYNTHETIC_CANARY")
    if synthetic_canary and synthetic_canary in f"{build.stdout or ''}\n{build.stderr or ''}":
        raise BrowserGateError("synthetic canary leaked into browser build output")
    if build.returncode != 0:
        detail = redact_output((build.stdout or "") + (build.stderr or ""), env)
        raise BrowserGateError(f"browser gate web build failed: {detail[-500:]}")
    for index, command in enumerate(
        integrated_playwright_commands(plan, between_spec_runs=between_spec_runs)
    ):
        if index > 0 and between_spec_runs is not None:
            between_spec_runs()
        playwright = run_command(
            command,
            env=plan.playwright_env,
            cwd=plan.platform_source / "apps/web",
        )
        if synthetic_canary and synthetic_canary in f"{playwright.stdout or ''}\n{playwright.stderr or ''}":
            raise BrowserGateError("synthetic canary leaked into browser runner output")
        if playwright.returncode != 0:
            detail = redact_output((playwright.stdout or "") + (playwright.stderr or ""), env)
            raise BrowserGateError(f"browser gate playwright failed: {detail[-500:]}")
    return plan.platform_revision


def run_standalone(plan: BrowserGatePlan, env: dict[str, str]) -> str:
    completed = run_command(plan.standalone_command, env=plan.playwright_env)
    if completed.returncode != 0:
        detail = redact_output((completed.stdout or "") + (completed.stderr or ""), env)
        raise BrowserGateError(f"browser gate harness failed: {detail[-500:]}")
    return plan.platform_revision



def platform_web_dir(platform_source: Path) -> Path:
    return platform_source / "apps/web"


def prepare_playwright_artifact_dirs(platform_source: Path) -> list[str]:
    """Remove stale Playwright artifacts so auth.setup starts from a clean storageState."""
    cleared: list[str] = []
    web_dir = platform_web_dir(platform_source)
    for relative in PLAYWRIGHT_ARTIFACT_DIRS:
        target = web_dir / relative
        if target.exists():
            shutil.rmtree(target)
            cleared.append(relative)
    return cleared


def run_full_web_e2e(
    *,
    repo_root: Path,
    env_file: Path,
    seed: dict[str, Any],
    allow_dev_head: bool = False,
) -> str:
    plan = build_plan(
        repo_root=repo_root,
        env_file=env_file,
        seed=seed,
        allow_dev_head=allow_dev_head,
        profile=FULL_WEB_PROFILE,
    )
    prepare_playwright_artifact_dirs(plan.platform_source)
    return run_integrated(plan, read_env(env_file), between_spec_runs=None)
def run_rollback_smoke(
    *,
    repo_root: Path,
    env_file: Path,
    seed: dict[str, Any],
    allow_dev_head: bool = False,
) -> str:
    plan = build_plan(
        repo_root=repo_root,
        env_file=env_file,
        seed=seed,
        allow_dev_head=allow_dev_head,
        profile=ROLLBACK_PROFILE,
    )
    prepare_playwright_artifact_dirs(plan.platform_source)
    return run_integrated(plan, read_env(env_file), between_spec_runs=None)



def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--env", required=True, type=Path)
    validate_parser.add_argument("--repo-root", required=True, type=Path)
    validate_parser.add_argument("--seed-receipt", default="{}")
    validate_parser.add_argument("--allow-dev-head", action="store_true")
    validate_parser.add_argument("--profile", default="phase1", choices=sorted(SUPPORTED_PROFILES))

    run_parser = subparsers.add_parser("run-integrated")
    run_parser.add_argument("--env", required=True, type=Path)
    run_parser.add_argument("--repo-root", required=True, type=Path)
    run_parser.add_argument("--seed-receipt", required=True)
    run_parser.add_argument("--allow-dev-head", action="store_true")
    run_parser.add_argument("--profile", default="phase1", choices=sorted(SUPPORTED_PROFILES))

    standalone_parser = subparsers.add_parser("run-standalone")
    standalone_parser.add_argument("--env", required=True, type=Path)
    standalone_parser.add_argument("--repo-root", required=True, type=Path)
    standalone_parser.add_argument("--seed-receipt", default="{}")
    standalone_parser.add_argument("--allow-dev-head", action="store_true")
    standalone_parser.add_argument("--profile", default="phase1", choices=sorted(SUPPORTED_PROFILES))

    full_web_parser = subparsers.add_parser("run-full-web-e2e")
    full_web_parser.add_argument("--env", required=True, type=Path)
    full_web_parser.add_argument("--repo-root", required=True, type=Path)
    full_web_parser.add_argument("--seed-receipt", required=True)
    full_web_parser.add_argument("--allow-dev-head", action="store_true")

    rollback_parser = subparsers.add_parser("run-rollback-smoke")
    rollback_parser.add_argument("--env", required=True, type=Path)
    rollback_parser.add_argument("--repo-root", required=True, type=Path)
    rollback_parser.add_argument("--seed-receipt", required=True)
    rollback_parser.add_argument("--allow-dev-head", action="store_true")

    args = parser.parse_args()
    seed = json.loads(args.seed_receipt)
    allow_dev_head = bool(args.allow_dev_head)
    profile = str(getattr(args, "profile", FULL_WEB_PROFILE))

    try:
        if args.command == "validate":
            plan = build_plan(
                repo_root=args.repo_root,
                env_file=args.env,
                seed=seed,
                allow_dev_head=allow_dev_head,
                profile=profile,
            )
            print(f"browser gate inventory: OK platform={plan.platform_revision} profile={profile}")
            return
        if args.command == "run-integrated":
            plan = build_plan(
                repo_root=args.repo_root,
                env_file=args.env,
                seed=seed,
                allow_dev_head=allow_dev_head,
                profile=profile,
            )
            revision = run_integrated(plan, read_env(args.env))
            print(f"browser gate integrated: OK platform={revision} profile={profile}")
            return
        if args.command == "run-standalone":
            plan = build_plan(
                repo_root=args.repo_root,
                env_file=args.env,
                seed=seed,
                allow_dev_head=allow_dev_head,
                profile=profile,
            )
            revision = run_standalone(plan, read_env(args.env))
            print(f"browser gate standalone: OK platform={revision} profile={profile}")
            return
        if args.command == "run-full-web-e2e":
            revision = run_full_web_e2e(
                repo_root=args.repo_root,
                env_file=args.env,
                seed=seed,
                allow_dev_head=allow_dev_head,
            )
            print(f"full-web e2e: OK platform={revision} profile={FULL_WEB_PROFILE}")
            return
        if args.command == "run-rollback-smoke":
            revision = run_rollback_smoke(
                repo_root=args.repo_root,
                env_file=args.env,
                seed=seed,
                allow_dev_head=allow_dev_head,
            )
            print(f"rollback smoke: OK platform={revision} profile={ROLLBACK_PROFILE}")
            return
        raise BrowserGateError(f"unsupported browser gate command: {args.command}")
    except BrowserGateError as error:
        print(f"browser gate: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
