#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
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


def resolve_platform_inventory_path(platform_source: Path, relative: str) -> Path:
    if relative.startswith(E2E_PREFIX):
        return platform_source / WEB_DIR_PREFIX / relative
    return platform_source / relative


def playwright_spec_argument(spec_path: str) -> str:
    if spec_path.startswith(WEB_DIR_PREFIX):
        return spec_path[len(WEB_DIR_PREFIX) :]
    if spec_path.startswith(E2E_PREFIX):
        return spec_path
    raise BrowserGateError("phase2 browser spec path must be web-relative")


def validate_inventory(platform_source: Path, manifest: dict[str, Any]) -> None:
    phase2_specs = manifest.get("phase2RequiredSpecs")
    if not isinstance(phase2_specs, list) or not phase2_specs:
        raise BrowserGateError("browser gate manifest phase2RequiredSpecs is invalid")
    invalid_phase2 = [
        relative
        for relative in phase2_specs
        if not isinstance(relative, str) or relative.startswith(WEB_DIR_PREFIX)
    ]
    if invalid_phase2:
        raise BrowserGateError("phase2 browser spec paths must be web-relative")
    missing_phase2 = [
        relative
        for relative in phase2_specs
        if not resolve_platform_inventory_path(platform_source, relative).is_file()
    ]
    if missing_phase2:
        raise BrowserGateError("phase 2 browser spec is missing from platform checkout")

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
    playwright_env = {
        "E2E_TEST_EMAIL": email,
        "E2E_TEST_PASSWORD": password,
        "E2E_SEED_USER_ID": user_id,
        "E2E_SEED_PROJECT_RUN_ID": project_run_id,
        "E2E_SEED_ROADMAP_ID": roadmap_id,
        "JAGALCHI_E2E_SEED_RUN_ID": project_run_id,
        "API_ORIGIN": "http://127.0.0.1:8080",
        "NEXT_PUBLIC_API_URL": "/api",
        "NEXT_PUBLIC_ENV": "development",
        "NEXT_PUBLIC_ANALYTICS_ENABLED": "false",
        "NEXT_PUBLIC_API_MOCKING": "false",
        "NEXT_PUBLIC_E2E_MOCKING": "false",
        "NEXT_PUBLIC_REALTIME_ENABLED": "true",
        "NEXT_PUBLIC_REALTIME_URL": "http://127.0.0.1:8080",
        "NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED": "true",
        "NEXT_PUBLIC_PROOF_PROFILE_ENABLED": "true",
        "NEXT_PUBLIC_SITE_URL": "http://127.0.0.1:3100",
    }
    if profile == "phase2":
        # Wave B entry routes compile to notFound() unless both flags are baked into `pnpm build`.
        playwright_env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"] = "true"
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
    validate_inventory(platform_source, manifest)
    lock = json.loads((repo_root / "deploy/local-stack.lock.json").read_text(encoding="utf-8"))
    if allow_dev_head is None:
        allow_dev_head = os.environ.get("JAGALCHI_DEV_HEAD", "") == "true"
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
    if profile == "phase2":
        phase2_specs = manifest.get("phase2RequiredSpecs")
        if not isinstance(phase2_specs, list) or not phase2_specs:
            raise BrowserGateError("phase2 browser manifest is incomplete")
        for spec in phase2_specs:
            integrated_playwright_command.append(playwright_spec_argument(str(spec)))
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
    if between_spec_runs is not None and len(specs) > 1:
        return [playwright_command_for_specs(base, [spec]) for spec in specs]
    return [base]


def run_integrated(
    plan: BrowserGatePlan,
    env: dict[str, str],
    *,
    between_spec_runs: Callable[[], None] | None = None,
) -> str:
    build = run_command(plan.integrated_build_command, env=plan.playwright_env)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--env", required=True, type=Path)
    validate_parser.add_argument("--repo-root", required=True, type=Path)
    validate_parser.add_argument("--seed-receipt", default="{}")
    validate_parser.add_argument("--allow-dev-head", action="store_true")

    run_parser = subparsers.add_parser("run-integrated")
    run_parser.add_argument("--env", required=True, type=Path)
    run_parser.add_argument("--repo-root", required=True, type=Path)
    run_parser.add_argument("--seed-receipt", required=True)
    run_parser.add_argument("--allow-dev-head", action="store_true")

    standalone_parser = subparsers.add_parser("run-standalone")
    standalone_parser.add_argument("--env", required=True, type=Path)
    standalone_parser.add_argument("--repo-root", required=True, type=Path)
    standalone_parser.add_argument("--seed-receipt", default="{}")
    standalone_parser.add_argument("--allow-dev-head", action="store_true")

    args = parser.parse_args()
    seed = json.loads(args.seed_receipt)

