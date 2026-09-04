from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from deploy.local_readiness import (
    ReadinessError,
    assert_api_ready,
    assert_stack_surface_ready,
    wait_for_post_seed_workflow_readiness,
    worker_heartbeat_epoch_ms,
)


class FakeHttp:
    def __init__(self, bodies: list[object] | None = None) -> None:
        self.bodies = list(bodies or [{"status": "ready"}])
        self.calls: list[tuple[str, str]] = []

    def request(self, method, target, *, body=None, headers=None, expected=(200,), follow_redirects=True):
        self.calls.append((method, target))
        if not self.bodies:
            raise RuntimeError("no more bodies")
        return mock.Mock(body=self.bodies.pop(0))


class FakeCommands:
    def __init__(self, outputs: list[str] | None = None, *, fail: bool = False) -> None:
        self.outputs = list(outputs or [""])
        self.commands: list[list[str]] = []
        self.fail = fail

    def run(self, command, *, check=True):
        self.commands.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, self.outputs.pop(0) if self.outputs else "", "")


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class LocalReadinessTest(unittest.TestCase):
    def test_assert_api_ready_rejects_non_ready_body(self) -> None:
        with self.assertRaisesRegex(ReadinessError, "not ready"):
            assert_api_ready(FakeHttp([{"status": "starting"}]))

    def test_worker_heartbeat_epoch_ms_parses_stdout(self) -> None:
        epoch = worker_heartbeat_epoch_ms(FakeCommands(["1700000000123.4"]), ["docker", "compose"])
        self.assertEqual(epoch, 1700000000123.4)

    def test_wait_requires_advancing_heartbeat(self) -> None:
        clock = FakeClock()
        commands = FakeCommands((["", "", "1000"] * 4))
        with self.assertRaisesRegex(ReadinessError, "did not advance"):
            wait_for_post_seed_workflow_readiness(
                FakeHttp([{"status": "ready"}] * 10),
                commands,
                ["docker", "compose"],
                {"WORKFLOW_HEARTBEAT_MS": "5000"},
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                timeout_seconds=2,
            )

    def test_wait_succeeds_when_heartbeat_advances(self) -> None:
        clock = FakeClock()
        commands = FakeCommands(["", "", "1000", "", "", "2000"])
        wait_for_post_seed_workflow_readiness(
            FakeHttp([{"status": "ready"}, {"status": "ready"}]),
            commands,
            ["docker", "compose"],
            {"WORKFLOW_HEARTBEAT_MS": "5000"},
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            timeout_seconds=5,
        )
        self.assertGreaterEqual(len(commands.commands), 2)

    def test_assert_stack_surface_ready_runs_worker_health_check(self) -> None:
        commands = FakeCommands([""])
        assert_stack_surface_ready(FakeHttp(), commands, ["docker", "compose", "-p", "jagalchi-v1-local"])
        joined = [" ".join(command) for command in commands.commands]
        self.assertTrue(any("health-check.js" in command for command in joined))


if __name__ == "__main__":
    unittest.main()
