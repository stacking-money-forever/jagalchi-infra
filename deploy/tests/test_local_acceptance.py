from __future__ import annotations

import subprocess
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from deploy.local_acceptance import HttpResponse, LocalAcceptance


ROOT = Path(__file__).resolve().parents[2]


def uid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.operations: dict[str, dict[str, object]] = {}
        self.next_id = 20
        self.upload_bytes = b""

    def request(self, method, target, *, body=None, headers=None, expected=(200,), follow_redirects=True):
        self.calls.append((method, target, body))
        response = self._response(method, target, body)
        if response.status not in expected:
            raise AssertionError((method, target, response.status, expected))
        return response

    def _new_operation(self, resource_type: str, resource_id: str) -> HttpResponse:
        operation_id = uid(self.next_id)
        self.next_id += 1
        self.operations[operation_id] = {
            "id": operation_id,
            "state": "SUCCEEDED",
            "result": {"resourceType": resource_type, "resourceId": resource_id},
        }
        return HttpResponse(202, {"id": operation_id})

    def _response(self, method: str, target: str, body: object) -> HttpResponse:
        if (method, target) == ("POST", "/users/auth/login"):
            return HttpResponse(200, {"accessToken": "secret", "user": {"id": uid(1)}})
        if method == "GET" and target in {f"/project-runs/{uid(2)}", f"/roadmaps/{uid(3)}"}:
            return HttpResponse(200, {"id": target.rsplit("/", 1)[1]})
        if (method, target) == ("POST", "/career/target-imports"):
            return self._new_operation("CAREER_TARGET_VERSION", uid(4))
        if (method, target) == ("GET", f"/career/target-versions/{uid(4)}"):
            return HttpResponse(200, {"id": uid(4), "careerTargetId": uid(5)})
        if (method, target) == ("POST", "/career/profile-snapshot-operations/github"):
            return self._new_operation("CANDIDATE_PROFILE_SNAPSHOT", uid(6))
        if (method, target) == ("GET", f"/career/profile-snapshots/{uid(6)}"):
            return HttpResponse(200, {"id": uid(6), "payload": {"repositories": [{"githubRepositoryId": "501"}]}})
        if (method, target) == ("POST", f"/career/profile-snapshots/{uid(6)}/confirm"):
            return HttpResponse(201, {"id": uid(7)})
        if (method, target) == ("POST", f"/career/targets/{uid(5)}/diff-snapshots"):
            return HttpResponse(201, {"id": uid(8)})
        if (method, target) == ("POST", f"/career/diff-snapshots/{uid(8)}/confirm"):
            return HttpResponse(201, {"id": uid(9)})
        if (method, target) == ("POST", f"/career/targets/{uid(5)}/project-proposal-operations"):
            return self._new_operation("PROJECT_PROPOSAL_SET", uid(10))
        if (method, target) == ("GET", f"/career/project-proposal-sets/{uid(10)}"):
            return HttpResponse(200, {"id": uid(10), "proposals": [{"id": uid(11)}, {"id": uid(12)}, {"id": uid(13)}]})
        if (method, target) == ("POST", "/project-run-operations"):
            return self._new_operation("PROJECT_RUN", uid(14))
        if (method, target) == ("GET", f"/project-runs/{uid(14)}"):
            task = {"id": "task-1"}
            return HttpResponse(200, {"id": uid(14), "plan": {"schemaVersion": 1}, "tasks": [task], "map": {"nodes": [task]}})
        if method == "GET" and target.startswith("/workflow-operations/"):
            return HttpResponse(200, self.operations[target.rsplit("/", 1)[1]])
        if (method, target) == ("POST", "/uploads"):
            return HttpResponse(201, {"id": uid(15), "uploadUrl": "http://127.0.0.1:9000/signed", "headers": {"content-type": "text/plain"}})
        if (method, target) == ("PUT", "http://127.0.0.1:9000/signed"):
            self.upload_bytes = body
            return HttpResponse(200)
        if (method, target) == ("POST", f"/uploads/{uid(15)}/complete"):
            return HttpResponse(201, {"id": uid(15), "status": "READY"})
        if (method, target) == ("GET", f"/uploads/{uid(15)}/content"):
            return HttpResponse(302, headers={"location": "http://127.0.0.1:9000/download"})
        if (method, target) == ("GET", "http://127.0.0.1:9000/download"):
            return HttpResponse(200, raw=self.upload_bytes)
        if (method, target) == ("DELETE", f"/uploads/{uid(15)}"):
            return HttpResponse(204)
        raise AssertionError((method, target, body))


class FakeCommands:
    def __init__(self, recovery_http=None) -> None:
        self.commands: list[list[str]] = []
        self.recovery_http = recovery_http

    def run(self, command, *, check=True):
        self.commands.append(command)
        if self.recovery_http and "up" in command and "workflow-worker" in command:
            self.recovery_http.restarted = True
        return subprocess.CompletedProcess(command, 0, "", "")


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        self.value += 0.1
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class RecoveryHttp:
    def __init__(self) -> None:
        self.restarted = False
        self.operation_id = uid(30)

    def request(self, method, target, *, body=None, headers=None, expected=(200,), follow_redirects=True):
        if (method, target) == ("POST", "/career/target-imports"):
            return HttpResponse(202, {"id": self.operation_id})
        if (method, target) == ("GET", f"/workflow-operations/{self.operation_id}"):
            state = "SUCCEEDED" if self.restarted else "RUNNING"
            return HttpResponse(200, {"id": self.operation_id, "state": state, "result": {"resourceType": "CAREER_TARGET_VERSION", "resourceId": uid(31)} if self.restarted else None})
        raise AssertionError((method, target))


class PendingHttp:
    def request(self, method, target, *, body=None, headers=None, expected=(200,), follow_redirects=True):
        return HttpResponse(200, {"id": uid(40), "state": "PENDING", "result": None})


def environment(api_source: Path, mode: str = "local") -> dict[str, str]:
    matrices = {
        "ci": ("fixture", "fixture", "fixture", "fake", "true", "true"),
        "ci-real-source": ("live", "fixture", "fixture", "fake", "true", "true"),
        "local": ("fixture", "fixture", "deepseek", "deepseek", "false", "false"),
        "local-real-source": ("live", "fixture", "deepseek", "deepseek", "false", "false"),
    }
    job, github, api_ai, ai_runtime, external, llm = matrices[mode]
    return {
        "LOCAL_SEED_EMAIL": "local@example.test",
        "LOCAL_SEED_PASSWORD": "local-password-123",
        "PLATFORM_SOURCE_DIR": str(api_source),
        "API_SOURCE_DIR": str(api_source),
        "AI_SOURCE_DIR": str(api_source),
        "JAGALCHI_LOCAL_MODE": mode,
        "JOB_SOURCE_PROVIDER": job,
        "GITHUB_PROVIDER": github,
        "AI_PROVIDER": api_ai,
        "AI_V1_PROVIDER": ai_runtime,
        "AI_DISABLE_EXTERNAL": external,
        "AI_DISABLE_LLM": llm,
    }


def contract_tree(root: Path, mode: str) -> tuple[Path, Path, Path, dict[str, str]]:
    infra = root / "infra"
    platform = root / "platform"
    api = root / "api"
    ai = root / "ai"
    schema = b'{"type":"object"}\n'
    schema_hash = hashlib.sha256(schema).hexdigest()
    openapi = b'{"openapi":"3.1.0"}\n'
    openapi_hash = hashlib.sha256(openapi).hexdigest()
    (infra / "deploy").mkdir(parents=True)
    (platform / "packages/api-client/contract").mkdir(parents=True)
    (api / "contracts/ai/v1").mkdir(parents=True)
    (ai / "contracts/ai/v1-generated").mkdir(parents=True)
    (platform / "packages/api-client/contract/openapi.json").write_bytes(openapi)
    (api / "contracts/openapi.json").write_bytes(openapi)
    (api / "contracts/ai/v1/schema.json").write_bytes(schema)
    (ai / "contracts/ai/v1-generated/schema.json").write_bytes(schema)
    (infra / "deploy/local-stack.lock.json").write_text(json.dumps({
        "project": "jagalchi-v1-local", "apiContractSha256": openapi_hash,
    }))
    (api / "contracts/ai/v1/manifest.json").write_text(json.dumps({
        "files": {"schema.json": schema_hash}, "bundleSha256": "b" * 64,
    }))
    (ai / "contracts/ai/v1-generated/manifest.json").write_text(json.dumps({
        "files": {"schema.json": schema_hash}, "aggregateSha256": "c" * 64,
    }))
    env = environment(api, mode)
    env.update({"PLATFORM_SOURCE_DIR": str(platform), "AI_SOURCE_DIR": str(ai)})
    if mode in {"ci-real-source", "local-real-source"}:
        env["REAL_JOB_SOURCE_URL"] = "https://jobs.example.test/role"
    return infra, api, ai, env


class ReceiptAcceptance(LocalAcceptance):
    def login_and_verify_seed(self) -> None:
        return None

    def run_fixture_path(self) -> None:
        return None

    def run_upload_lifecycle(self) -> None:
        return None

    def run_worker_recovery(self) -> None:
        return None


class LocalAcceptanceTests(unittest.TestCase):
    def test_accepts_exact_local_real_source_matrix_and_uses_real_url(self) -> None:
        env = environment(ROOT, "local-real-source")
        env["REAL_JOB_SOURCE_URL"] = "https://jobs.example.test/role"
        acceptance = LocalAcceptance(
            FakeHttp(), FakeCommands(), env,
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose"], ROOT,
        )
        acceptance.validate_environment()
        self.assertEqual(acceptance.job_source_url(), "https://jobs.example.test/role")

        env["AI_V1_PROVIDER"] = "fake"
        with self.assertRaisesRegex(RuntimeError, "locked Phase 1 provider mode"):
            acceptance.validate_environment()

    def test_ci_real_source_receipt_does_not_claim_live_ai_or_expose_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            infra, api, _, env = contract_tree(Path(directory), "ci-real-source")
            acceptance = ReceiptAcceptance(
                FakeHttp(), FakeCommands(), env,
                {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
                ["docker", "compose"], api, repo_root=infra,
            )
            acceptance.run()
            receipts = list((infra / ".evidence").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text())
            self.assertEqual(receipt["mode"], "ci-real-source")
            self.assertTrue(receipt["claims"]["realJobSource"])
            self.assertTrue(receipt["claims"]["fakeAi"])
            self.assertFalse(receipt["claims"]["liveDeepSeek"])
            self.assertEqual(receipt["contractHashes"]["apiOpenApiSha256"], hashlib.sha256(b'{"openapi":"3.1.0"}\n').hexdigest())
            self.assertEqual(os.stat(receipts[0]).st_mode & 0o777, 0o600)
            serialized = json.dumps(receipt)
            for forbidden in (uid(1), uid(2), uid(3), "local@example.test", "local-password-123", "accessToken", "payload"):
                self.assertNotIn(forbidden, serialized)

    def test_receipt_is_written_only_after_every_gate_succeeds(self) -> None:
        class FailingAcceptance(ReceiptAcceptance):
            def run_upload_lifecycle(self) -> None:
                raise RuntimeError("synthetic gate failure")

        with tempfile.TemporaryDirectory() as directory:
            infra, api, _, env = contract_tree(Path(directory), "local")
            acceptance = FailingAcceptance(
                FakeHttp(), FakeCommands(), env,
                {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
                ["docker", "compose"], api, repo_root=infra,
            )
            with self.assertRaisesRegex(RuntimeError, "synthetic gate failure"):
                acceptance.run()
            self.assertFalse((infra / ".evidence").exists())

    def test_local_real_source_receipt_claims_live_deepseek_only_for_exact_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            infra, api, _, env = contract_tree(Path(directory), "local-real-source")
            acceptance = ReceiptAcceptance(
                FakeHttp(), FakeCommands(), env,
                {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
                ["docker", "compose"], api, repo_root=infra,
            )
            acceptance.run()
            receipt_path = next((infra / ".evidence").glob("*.json"))
            receipt = json.loads(receipt_path.read_text())
            self.assertTrue(receipt["claims"]["realJobSource"])
            self.assertTrue(receipt["claims"]["liveDeepSeek"])
            self.assertFalse(receipt["claims"]["fakeAi"])

    def test_full_fixture_path_and_upload_use_only_backend_resources(self) -> None:
        http = FakeHttp()
        acceptance = LocalAcceptance(
            http, FakeCommands(), environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"], ROOT,
        )
        acceptance.validate_environment()
        acceptance.login_and_verify_seed()
        acceptance.run_fixture_path()
        acceptance.run_upload_lifecycle()

        proposal_call = next(call for call in http.calls if call[:2] == ("POST", f"/career/targets/{uid(5)}/project-proposal-operations"))
        self.assertEqual(proposal_call[2], {"careerDiffSnapshotId": uid(9), "constraints": {"availableHours": 20, "preferredStack": ["typescript"], "allowedRepositoryModes": ["EXISTING_OWNED"]}})
        self.assertIn(("DELETE", f"/uploads/{uid(15)}", None), http.calls)
        self.assertTrue(acceptance.resource_ids.isdisjoint(acceptance.client_ids))

    def test_worker_recovery_uses_sigkill_safe_timings_and_restores_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            api_source = Path(directory)
            worker = api_source / "src/workflow-operations/workflow-operation.worker.ts"
            config = api_source / "src/shared/config/environment.ts"
            worker.parent.mkdir(parents=True)
            config.parent.mkdir(parents=True)
            worker.write_text("WORKFLOW_HOLD_AFTER_CLAIM_MS")
            config.write_text("WORKFLOW_HOLD_AFTER_CLAIM_MS is not allowed in production")
            http = RecoveryHttp()
            commands = FakeCommands(http)
            clock = FakeClock()
            acceptance = LocalAcceptance(
                http, commands, environment(api_source),
                {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
                ["docker", "compose", "-p", "jagalchi-v1-local"], api_source,
                monotonic=clock.monotonic, sleep=clock.sleep,
            )

            acceptance.run_worker_recovery()

            flattened = [" ".join(command) for command in commands.commands]
            self.assertTrue(any("docker kill --signal=KILL" in command for command in flattened))
            held = next(command for command in flattened if "WORKFLOW_HOLD_AFTER_CLAIM_MS=12000" in command)
            self.assertIn("AI_TIMEOUT_MS=1000", held)
            self.assertIn("WORKFLOW_LEASE_MS=10000", held)
            self.assertGreaterEqual(sum("up -d --no-deps workflow-worker" in command for command in flattened), 1)

    def test_operation_polling_deadline_fails_closed(self) -> None:
        clock = FakeClock()
        acceptance = LocalAcceptance(
            PendingHttp(), FakeCommands(), environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"], ROOT,
            monotonic=clock.monotonic, sleep=clock.sleep,
        )
        with self.assertRaisesRegex(RuntimeError, "polling deadline"):
            acceptance.poll_operation(uid(40), timeout_seconds=1)

    def test_shell_entrypoint_has_exact_optional_reset_guard_and_parses(self) -> None:
        script = ROOT / "deploy/local-acceptance.sh"
        subprocess.run(["bash", "-n", str(script)], check=True)
        source = script.read_text()
        self.assertIn('"$reset" == "--reset"', source)
        self.assertIn('"--confirm=$project_name"', source)
        self.assertIn("--reset-performed", source)
        self.assertNotIn("local-reset.sh --confirm", source)


if __name__ == "__main__":
    unittest.main()
