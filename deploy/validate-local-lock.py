#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(message)


def repository_slug(source_dir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(source_dir), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    )
    remote = result.stdout.strip().removesuffix(".git")
    match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+)$", remote)
    if match is None:
        fail(f"unsupported origin URL for canonical source: {remote}")
    return match.group(1)


def validate_source(name: str, source_dir: Path, contract: dict[str, object]) -> None:
    if not source_dir.is_dir() or not ((source_dir / ".git").is_dir() or (source_dir / ".git").is_file()):
        fail(f"not a git checkout: {source_dir}")
    expected_repository = contract.get("repository")
    if repository_slug(source_dir) != expected_repository:
        fail(f"wrong {name} repository: expected {expected_repository}")
    required_files = contract.get("requiredFiles")
    if not isinstance(required_files, list) or not required_files:
        fail(f"{name} requiredFiles must be a non-empty list")
    for relative in required_files:
        if not isinstance(relative, str) or not (source_dir / relative).is_file():
            fail(f"missing required {name} source file: {relative}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--platform-source", required=True, type=Path)
    parser.add_argument("--api-source", required=True, type=Path)
    parser.add_argument("--ai-source", required=True, type=Path)
    args = parser.parse_args()

    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    if lock.get("schemaVersion") != 1 or lock.get("project") != "jagalchi-v1-local":
        fail("unsupported local stack lock identity")

    expected_python = str(lock.get("python"))
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_python != expected_python:
        fail(f"Python {expected_python} is required; found {actual_python}")

    compose_path = args.repo_root / str(lock.get("composeFile"))
    if not compose_path.is_file():
        fail(f"missing locked Compose file: {compose_path}")
    compose = compose_path.read_text(encoding="utf-8")
    if not re.search(r"^name:\s*jagalchi-v1-local\s*$", compose, re.MULTILINE):
        fail("Compose project name differs from the local stack lock")

    forbidden = lock.get("forbiddenSourceFragments")
    if not isinstance(forbidden, list):
        fail("forbiddenSourceFragments must be a list")
    source_pair = f"{args.platform_source.resolve()}:{args.api_source.resolve()}:{args.ai_source.resolve()}"
    for fragment in forbidden:
        if not isinstance(fragment, str) or fragment in source_pair:
            fail(f"forbidden source checkout: {fragment}")

    canonical = lock.get("canonicalSources")
    if not isinstance(canonical, dict):
        fail("canonicalSources must be an object")
    for key, path in (("platform", args.platform_source), ("api", args.api_source), ("ai", args.ai_source)):
        contract = canonical.get(key)
        if not isinstance(contract, dict):
            fail(f"missing canonical source contract: {key}")
        validate_source(key, path, contract)

    openapi_path = args.api_source / "contracts" / "openapi.json"
    if not openapi_path.is_file():
        fail(f"missing API contract: {openapi_path}")
    actual_contract_sha = hashlib.sha256(openapi_path.read_bytes()).hexdigest()
    if actual_contract_sha != lock.get("apiContractSha256"):
        fail("API OpenAPI contract hash differs from local stack lock")

    providers = lock.get("providers")
    if not isinstance(providers, dict) or not isinstance(providers.get("modes"), dict):
        fail("providers and providers.modes must be objects")
    mode = os.environ.get("JAGALCHI_LOCAL_MODE", "")
    expected_matrix = providers["modes"].get(mode)
    if not isinstance(expected_matrix, list) or len(expected_matrix) != 6:
        fail(f"unsupported JAGALCHI_LOCAL_MODE: {mode}")
    actual_matrix = [
        os.environ.get("JOB_SOURCE_PROVIDER", ""),
        os.environ.get("GITHUB_PROVIDER", ""),
        os.environ.get("AI_PROVIDER", ""),
        os.environ.get("AI_V1_PROVIDER", ""),
        os.environ.get("AI_DISABLE_EXTERNAL", "").lower(),
        os.environ.get("AI_DISABLE_LLM", "").lower(),
    ]
    if actual_matrix != expected_matrix:
        fail(f"provider matrix differs from locked {mode} mode")
    if os.environ.get("DEEPSEEK_BASE_URL") != providers.get("deepseekBaseUrl"):
        fail("DeepSeek base URL differs from lock")
    if os.environ.get("DEEPSEEK_EXTRACTION_MODEL") != providers.get("extractionModel"):
        fail("DeepSeek extraction model differs from lock")
    if os.environ.get("DEEPSEEK_PLANNING_MODEL") != providers.get("planningModel"):
        fail("DeepSeek planning model differs from lock")
    if expected_matrix[3] == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
        fail("DEEPSEEK_API_KEY is required outside offline mode")

    loopback_ports = lock.get("loopbackPorts")
    if not isinstance(loopback_ports, list) or not all(isinstance(port, int) for port in loopback_ports):
        fail("loopbackPorts must be an integer list")
    actual_ports = sorted({int(port) for port in re.findall(r"127\.0\.0\.1:(\d+):\d+", compose)})
    if actual_ports != sorted(loopback_ports):
        fail(f"Compose loopback ports differ from lock: {actual_ports}")
    unsafe_bind = re.search(r"(?:^|[\"'\s-])(?:0\.0\.0\.0|::|\[::\]):\d+:\d+", compose)
    if unsafe_bind:
        fail("Compose contains a non-loopback published port")

    print("local stack lock: OK")


if __name__ == "__main__":
    main()
