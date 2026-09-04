#!/usr/bin/env python3
"""Deterministic purge of stale Jagalchi local-stack containers after compose down.

``docker compose down`` only removes containers Docker Compose still associates with
the project label. When compose metadata drifts (missing or stale labels, an old
config-hash, or a container recreated outside the current compose state), a
canonical ``{project}-{service}-1`` name can survive ``down`` and block the next
``up`` with a name conflict.

Reset therefore derives an explicit allowlist of container names from the locked
compose config and force-removes only those names after ``compose down``, after
verifying each target is a disposable Jagalchi local acceptance container.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

InspectFn = Callable[[str], dict[str, Any] | None]
RemoveFn = Callable[[str], None]
ComposeServicesFn = Callable[[Path, Path, str], list[str]]


class ResetError(RuntimeError):
    pass


def load_lock(repo_root: Path) -> dict[str, Any]:
    lock_path = repo_root / "deploy" / "local-stack.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    project = lock.get("project")
    compose_file = lock.get("composeFile")
    if project != "jagalchi-v1-local" or not isinstance(compose_file, str):
        raise ResetError("unsupported local stack lock identity")
    return lock


def compose_services(compose_file: Path, env_file: Path, project_name: str) -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            project_name,
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "config",
            "--services",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def allowlisted_container_names(project_name: str, services: list[str]) -> list[str]:
    if project_name != "jagalchi-v1-local":
        raise ResetError(f"unsafe compose project: {project_name}")
    return sorted(f"{project_name}-{service}-1" for service in services)


def normalize_container_name(name: str) -> str:
    return name.lstrip("/")


def disposal_decision(
    container_name: str,
    labels: dict[str, str],
    *,
    allowlist: set[str],
    project_name: str,
    compose_file: Path,
) -> tuple[bool, str]:
    normalized = normalize_container_name(container_name)
    if normalized not in allowlist:
        return False, "not in compose-derived allowlist"

    compose_project = labels.get("com.docker.compose.project", "")
    if compose_project and compose_project != project_name:
        return False, f"foreign compose project label: {compose_project}"

    config_files = labels.get("com.docker.compose.project.config_files", "")
    if config_files:
        configured = Path(config_files)
        try:
            configured_resolved = configured.resolve()
        except OSError:
            configured_resolved = configured
        expected = compose_file.resolve()
        if configured_resolved != expected:
            return False, f"compose config-files points elsewhere: {config_files}"

    working_dir = labels.get("com.docker.compose.project.working_dir", "")
    if working_dir:
        try:
            if compose_file.parent.resolve() != Path(working_dir).resolve():
                return False, f"compose working_dir mismatch: {working_dir}"
        except OSError:
            return False, f"compose working_dir unreadable: {working_dir}"

    return True, "disposable jagalchi local acceptance container"


def docker_inspect(container_name: str) -> dict[str, Any] | None:
    normalized = normalize_container_name(container_name)
    result = subprocess.run(
        ["docker", "inspect", normalized],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    if not payload:
        return None
    return payload[0]


def docker_remove(container_name: str) -> None:
    normalized = normalize_container_name(container_name)
    subprocess.run(["docker", "rm", "-f", normalized], check=True, capture_output=True, text=True)


def purge_stale_allowlisted_containers(
    *,
    repo_root: Path,
    env_file: Path,
    inspect_container: InspectFn = docker_inspect,
    remove_container: RemoveFn = docker_remove,
    discover_services: ComposeServicesFn = compose_services,
) -> list[str]:
    lock = load_lock(repo_root)
    project_name = str(lock["project"])
    compose_file = (repo_root / str(lock["composeFile"])).resolve()
    if not compose_file.is_file():
        raise ResetError(f"missing locked compose file: {compose_file}")
    if not env_file.is_file():
        raise ResetError(f"missing env file: {env_file}")

    services = discover_services(compose_file, env_file, project_name)
    allowlist = set(allowlisted_container_names(project_name, services))
    removed: list[str] = []

    for container_name in sorted(allowlist):
        inspection = inspect_container(container_name)
        if inspection is None:
            continue
        labels = inspection.get("Config", {}).get("Labels", {}) or {}
        disposable, reason = disposal_decision(
            container_name,
            labels,
            allowlist=allowlist,
            project_name=project_name,
            compose_file=compose_file,
        )
        if not disposable:
            raise ResetError(f"refusing stale-container purge for {container_name}: {reason}")
        remove_container(container_name)
        removed.append(container_name)

    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["purge-stale"])
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "purge-stale":
        removed = purge_stale_allowlisted_containers(repo_root=args.repo_root.resolve(), env_file=args.env_file.resolve())
        if removed:
            print(f"purged stale allowlisted containers: {', '.join(removed)}")
        else:
            print("no stale allowlisted containers to purge")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ResetError as error:
        print(f"local stack reset: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
