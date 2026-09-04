#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol


class ReadinessError(RuntimeError):
    pass


class HttpProbe(Protocol):
    def request(
        self,
        method: str,
        target: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
        follow_redirects: bool = True,
    ) -> Any: ...


class CommandProbe(Protocol):
    def run(self, command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]: ...


WORKER_HEARTBEAT_PROBE = r"""
const { AppDataSource } = require('./dist/database/data-source');
(async () => {
  await AppDataSource.initialize();
  try {
    const rows = await AppDataSource.query(
      'SELECT EXTRACT(EPOCH FROM MAX(heartbeat_at)) * 1000 AS ms FROM workflow_worker_heartbeats'
    );
    const ms = rows[0]?.ms;
    process.stdout.write(ms == null || ms === '' ? '' : String(Number(ms)));
  } finally {
    if (AppDataSource.isInitialized) await AppDataSource.destroy();
  }
})().catch(() => process.exit(1));
""".strip()


def parse_ready_body(body: Any) -> None:
    if not isinstance(body, dict) or body.get("status") != "ready":
        raise ReadinessError("API /health/ready is not ready")


def worker_heartbeat_epoch_ms(commands: CommandProbe, compose: list[str]) -> float | None:
    try:
        result = commands.run(
            [
                *compose,
                "exec",
                "-T",
                "workflow-worker",
                "node",
                "-e",
                WORKER_HEARTBEAT_PROBE,
            ]
        )
    except subprocess.CalledProcessError as error:
        raise ReadinessError("workflow worker heartbeat probe failed") from error
    text = result.stdout.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as error:
        raise ReadinessError("workflow worker heartbeat probe returned malformed output") from error


def assert_api_ready(http: HttpProbe) -> None:
    try:
        response = http.request("GET", "/health/ready", expected=(200,))
    except Exception as error:
        raise ReadinessError("API /health/ready is unreachable") from error
    parse_ready_body(response.body)


def assert_ai_ready(commands: CommandProbe, compose: list[str]) -> None:
    try:
        commands.run(
            [
                *compose,
                "exec",
                "-T",
                "ai",
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "5",
                "http://127.0.0.1:8000/ai/health/",
            ]
        )
    except subprocess.CalledProcessError as error:
        raise ReadinessError("Django AI /ai/health/ is not ready") from error


def assert_worker_health_check(commands: CommandProbe, compose: list[str]) -> None:
    try:
        commands.run(
            [
                *compose,
                "exec",
                "-T",
                "workflow-worker",
                "node",
                "dist/workflow/health-check.js",
            ]
        )
    except subprocess.CalledProcessError as error:
        raise ReadinessError("workflow worker health-check.js failed") from error


def assert_stack_surface_ready(http: HttpProbe, commands: CommandProbe, compose: list[str]) -> None:
    assert_api_ready(http)
    assert_ai_ready(commands, compose)
    assert_worker_health_check(commands, compose)


def wait_for_post_seed_workflow_readiness(
    http: HttpProbe,
    commands: CommandProbe,
    compose: list[str],
    env: dict[str, str],
    *,
    monotonic,
    sleep,
    timeout_seconds: int = 60,
) -> None:
    deadline = monotonic() + timeout_seconds
    heartbeat_ms = max(1000, int(env.get("WORKFLOW_HEARTBEAT_MS", "5000")))
    poll_seconds = min(0.5, heartbeat_ms / 4000)
    baseline: float | None = None
    last_failure = "workflow stack did not become ready after seed"

    while monotonic() < deadline:
        try:
            assert_stack_surface_ready(http, commands, compose)
            epoch = worker_heartbeat_epoch_ms(commands, compose)
        except ReadinessError as error:
            last_failure = str(error)
            sleep(poll_seconds)
            continue

        if epoch is None:
            last_failure = "workflow worker heartbeat is missing"
            sleep(poll_seconds)
            continue
        if baseline is None:
            baseline = epoch
            sleep(poll_seconds)
            continue
        if epoch > baseline:
            return
        last_failure = "workflow worker heartbeat did not advance after seed"
        sleep(poll_seconds)

    raise ReadinessError(last_failure)


def smoke_ready_body(raw: str) -> None:
    body = json.loads(raw)
    if body.get("status") != "ready":
        raise ReadinessError("API /health/ready body is not ready")
