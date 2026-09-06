from __future__ import annotations

import subprocess
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deploy.local_acceptance import (
    AcceptanceError,
    AI_ROUTE_PATHS,
    HttpResponse,
    LocalAcceptance,
    SEED_EVIDENCE_RULES,
    SEED_TASK_KEY,
    effective_environment,
)


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
            return HttpResponse(200, {"id": uid(14), "version": 1, "plan": {"schemaVersion": 1}, "tasks": [task], "map": {"nodes": [task]}})
        if (method, target) == ("POST", f"/project-runs/{uid(14)}/tasks/task-1/start"):
            return HttpResponse(201, {"id": uid(14), "version": 2, "currentTaskId": "task-1"})
        if (method, target) == ("POST", f"/project-runs/{uid(14)}/tasks/task-1/ai-help"):
            return HttpResponse(200, {
                "guidance": "Start with the cited requirement.",
                "provenance": {
                    "provider": "fake",
                    "model": "fake-v1",
                    "promptVersion": "focus-task-help-v1",
                    "inputHash": "a" * 64,
                    "generatedAt": "2026-09-05T00:00:00Z",
                },
            })
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
    def __init__(self, recovery_http=None, *, fail: bool = False, outputs: list[str] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.recovery_http = recovery_http
        self.fail = fail
        self.outputs = list(outputs or [])

    def run(self, command, *, check=True):
        self.commands.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(1, command)
        if self.recovery_http and "up" in command and "workflow-worker" in command:
            self.recovery_http.restarted = True
        stdout = self.outputs.pop(0) if self.outputs else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

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

    def wait_for_post_seed_workflow_readiness(self, timeout_seconds: int = 60) -> None:
        return None

    def run_fixture_path(self) -> None:
        return None

    def run_upload_lifecycle(self) -> None:
        return None

    def run_worker_recovery(self) -> None:
        return None

    def run_task_verification_proof(self) -> None:
        return None

    def run_restart_retention(self) -> None:
        return None


class ProofHttp:
    HEAD_SHA = "a" * 40

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.version = 1
        self.task_state = "READY"
        self.run_state = "ACTIVE"
        self.publication_state = "UNPUBLISHED"
        self.snapshot_id: str | None = None
        self.reverified_snapshot_id: str | None = None
        self.operations: dict[str, dict[str, object]] = {}
        self.next_operation = 60
        self.calls: list[tuple[str, str, object]] = []

    def _evaluations(self) -> list[dict[str, object]]:
        return [
            {"ruleId": "rule-0", "type": "MERGED_PR", "passed": True, "code": "PASS"},
            {"ruleId": "rule-1", "type": "CHANGED_PATH", "passed": True, "code": "PASS"},
            {"ruleId": "rule-2", "type": "NAMED_CHECK", "passed": True, "code": "PASS"},
        ]

    def _projection(self) -> dict[str, object]:
        proof = None
        if self.snapshot_id is not None:
            proof = {
                "summary": "Verified",
                "validUntil": None,
                "publication": {"state": self.publication_state, "publicId": "proof-public" if self.publication_state == "ACTIVE" else None},
                "verification": {"state": "PASS", "verifiedAt": "2026-09-03T00:00:00.000Z"},
                "facts": {
                    "snapshotId": self.reverified_snapshot_id or self.snapshot_id,
                    "verificationLevel": "MACHINE_VERIFIED",
                    "provider": "fixture",
                    "repositoryId": "9000001",
                    "pullNumber": 42,
                    "headSha": self.HEAD_SHA,
                    "observedAt": "2026-09-03T00:00:00.000Z",
                    "evaluations": self._evaluations(),
                },
            }
        return {
            "id": self.run_id,
            "version": self.version,
            "state": self.run_state,
            "repositoryBinding": {"headSha": self.HEAD_SHA},
            "tasks": [{
                "id": SEED_TASK_KEY,
                "state": self.task_state,
                "evidenceRequirements": list(SEED_EVIDENCE_RULES),
            }],
            "proof": proof,
        }

    def request(self, method, target, *, body=None, headers=None, expected=(200,), follow_redirects=True):
        self.calls.append((method, target, body))
        response = self._response(method, target)
        if response.status not in expected:
            raise AssertionError((method, target, response.status, expected))
        return response

    def _response(self, method: str, target: str) -> HttpResponse:
        if method == "GET" and target == "/health/ready":
            return HttpResponse(200, {"status": "ready", "service": "jagalchi-api"})
        if method == "GET" and target == f"/project-runs/{self.run_id}":
            return HttpResponse(200, self._projection())
        if method == "POST" and target == f"/project-runs/{self.run_id}/tasks/{SEED_TASK_KEY}/start":
            self.version += 1
            self.task_state = "IN_PROGRESS"
            self.run_state = "ACTIVE"
            return HttpResponse(201, self._projection())
        if method == "POST" and target == f"/project-runs/{self.run_id}/tasks/{SEED_TASK_KEY}/verify":
            operation_id = uid(self.next_operation)
            self.next_operation += 1
            self.version += 1
            self.task_state = "VERIFYING"
            self.operations[operation_id] = {
                "id": operation_id,
                "state": "PENDING",
                "result": None,
                "kind": "TASK_VERIFICATION",
            }
            body = self._projection()
            body["operationId"] = operation_id
            return HttpResponse(202, body)
        if method == "GET" and target.startswith("/workflow-operations/"):
            operation_id = target.rsplit("/", 1)[1]
            operation = self.operations[operation_id]
            if operation["state"] == "PENDING":
                if operation.get("kind") == "PROOF_REVERIFICATION":
                    self.version += 1
                    self.reverified_snapshot_id = uid(51)
                    operation["state"] = "SUCCEEDED"
                    operation["result"] = {
                        "resourceType": "PROOF_SNAPSHOT",
                        "resourceId": self.reverified_snapshot_id,
                    }
                else:
                    self.version += 1
                    self.task_state = "DONE"
                    self.run_state = "COMPLETED"
                    self.snapshot_id = uid(50)
                    operation["state"] = "SUCCEEDED"
                    operation["result"] = {
                        "resourceType": "PROJECT_TASK",
                        "resourceId": uid(70),
                        "proofSnapshotId": self.snapshot_id,
                        "status": "PASS",
                    }
            return HttpResponse(200, operation)
        if method == "POST" and target == f"/project-runs/{self.run_id}/publish":
            self.version += 1
            self.publication_state = "ACTIVE"
            return HttpResponse(201, self._projection())
        if method == "POST" and target == f"/project-runs/{self.run_id}/reverify":
            operation_id = uid(self.next_operation)
            self.next_operation += 1
            self.operations[operation_id] = {
                "id": operation_id,
                "state": "PENDING",
                "result": None,
                "kind": "PROOF_REVERIFICATION",
            }
            return HttpResponse(202, {"id": operation_id, "kind": "PROOF_REVERIFICATION", "state": "PENDING"})
        raise AssertionError((method, target))


class RestartHttp(ProofHttp):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.snapshot_id = uid(50)
        self.reverified_snapshot_id = uid(51)
        self.publication_state = "ACTIVE"
        self.task_state = "DONE"
        self.run_state = "COMPLETED"
        self.import_operation: str | None = None
        self.import_polls = 0

    def _response(self, method: str, target: str) -> HttpResponse:
        if (method, target) == ("POST", "/career/target-imports"):
            self.import_operation = uid(80)
            self.operations[self.import_operation] = {"id": self.import_operation, "state": "RUNNING", "result": None}
            return HttpResponse(202, {"id": self.import_operation})
        if method == "GET" and self.import_operation and target == f"/workflow-operations/{self.import_operation}":
            self.import_polls += 1
            if self.import_polls < 2:
                return HttpResponse(200, self.operations[self.import_operation])
            self.operations[self.import_operation] = {
                "id": self.import_operation,
                "state": "SUCCEEDED",
                "result": {"resourceType": "CAREER_TARGET_VERSION", "resourceId": uid(81)},
            }
            return HttpResponse(200, self.operations[self.import_operation])
        return super()._response(method, target)


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
            self.assertEqual(receipt["receiptVersion"], 3)
            self.assertEqual(receipt["mode"], "ci-real-source")
            self.assertTrue(receipt["claims"]["realJobSource"])
            self.assertTrue(receipt["claims"]["fixtureApiAi"])
            self.assertTrue(receipt["claims"]["fakeAiRuntime"])
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
            self.assertFalse(receipt["claims"]["fixtureApiAi"])
            self.assertFalse(receipt["claims"]["fakeAiRuntime"])

    def test_receipt_distinguishes_api_ai_from_runtime_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            infra, api, _, env = contract_tree(Path(directory), "local")
            env["AI_V1_PROVIDER"] = "fake"
            env["AI_DISABLE_EXTERNAL"] = "true"
            env["AI_DISABLE_LLM"] = "true"
            acceptance = ReceiptAcceptance(
                FakeHttp(), FakeCommands(), env,
                {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
                ["docker", "compose"], api, repo_root=infra,
            )
            receipt_path = acceptance.write_receipt()
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["providerEvidence"]["apiAi"], "deepseek")
            self.assertEqual(receipt["providerEvidence"]["aiRuntime"], "fake")
            self.assertFalse(receipt["claims"]["fixtureApiAi"])
            self.assertTrue(receipt["claims"]["fakeAiRuntime"])
            self.assertFalse(receipt["claims"]["liveDeepSeek"])

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
    def test_fixture_path_invokes_connected_focus_help_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "src").mkdir()
            (source / "src/focus.ts").write_text("focus-task-help", encoding="utf-8")
            (source / "jagalchi_ai").mkdir()
            (source / "jagalchi_ai/focus.py").write_text("focusTaskHelp", encoding="utf-8")
            http = FakeHttp()
            env = environment(source)
            env["AI_SOURCE_DIR"] = str(source)
            acceptance = LocalAcceptance(
                http,
                FakeCommands(),
                env,
                {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
                ["docker", "compose", "-p", "jagalchi-v1-local"],
                source,
            )

            acceptance.run_fixture_path()

            self.assertIn(
                ("POST", f"/project-runs/{uid(14)}/tasks/task-1/start", {}),
                http.calls,
            )
            self.assertIn(
                (
                    "POST",
                    f"/project-runs/{uid(14)}/tasks/task-1/ai-help",
                    {
                        "question": (
                            "What is the next evidence-backed step? "
                            f"{acceptance.synthetic_canary}"
                        )
                    },
                ),
                http.calls,
            )
            self.assertEqual(
                acceptance.fixture_documents["projectRun"]["focusTaskHelpReceipt"]["provider"],
                "fake",
            )

    def test_synthetic_canary_scan_records_only_safe_metadata(self) -> None:
        acceptance = LocalAcceptance(
            FakeHttp(),
            FakeCommands(outputs=["api ready\nworker ready\nai ready"]),
            environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"],
            ROOT,
            profile="phase2",
        )
        acceptance.run_synthetic_canary_non_disclosure()
        self.assertEqual(
            acceptance.synthetic_canary_evidence,
            {
                "scheme": "per-run-synthetic-canary",
                "scannedSurfaces": ["aiRouteReceipts", "fixtureDocuments", "serviceLogs"],
                "matches": 0,
            },
        )

    def test_synthetic_canary_scan_fails_without_echoing_the_value(self) -> None:
        acceptance = LocalAcceptance(
            FakeHttp(),
            FakeCommands(),
            environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"],
            ROOT,
            profile="phase2",
        )
        acceptance.commands = FakeCommands(
            outputs=[f"unsafe log {acceptance.synthetic_canary}"]
        )
        with self.assertRaisesRegex(
            AcceptanceError,
            "synthetic canary disclosure detected",
        ) as captured:
            acceptance.run_synthetic_canary_non_disclosure()
        self.assertNotIn(acceptance.synthetic_canary, str(captured.exception))

    def test_phase2_receipt_fails_closed_without_canary_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            infra, api, _, env = contract_tree(Path(directory), "ci")
            acceptance = ReceiptAcceptance(
                FakeHttp(),
                FakeCommands(),
                env,
                {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
                ["docker", "compose"],
                api,
                repo_root=infra,
                profile="phase2",
            )
            with self.assertRaisesRegex(
                AcceptanceError,
                "requires synthetic canary evidence",
            ):
                acceptance.write_receipt()

    def test_phase2_accepts_explicit_deterministic_runtime_override(self) -> None:
        env = environment(ROOT, "local-real-source")
        env["REAL_JOB_SOURCE_URL"] = "https://jobs.example.test/role"
        with mock.patch.dict(
            os.environ,
            {
                "AI_V1_PROVIDER": "fake",
                "AI_DISABLE_EXTERNAL": "true",
                "AI_DISABLE_LLM": "true",
            },
        ):
            env = effective_environment(env)
        acceptance = LocalAcceptance(
            FakeHttp(),
            FakeCommands(),
            env,
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"],
            ROOT,
            profile="phase2",
        )

        acceptance.validate_environment()

    def test_ai_http_receipt_collection_requires_every_v1_route(self) -> None:
        logs = "\n".join(
            f'"POST {path} HTTP/1.1" 200 42' for path in AI_ROUTE_PATHS.values()
        )
        acceptance = LocalAcceptance(
            FakeHttp(),
            FakeCommands(outputs=[logs]),
            environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"],
            ROOT,
        )
        observed = acceptance.collect_ai_http_receipts(AI_ROUTE_PATHS)
        self.assertEqual(set(observed), set(AI_ROUTE_PATHS))
        self.assertTrue(all(item["httpStatus"] == 200 for item in observed.values()))

        missing_logs = "\n".join(
            f'"POST {path} HTTP/1.1" 200 42'
            for route, path in AI_ROUTE_PATHS.items()
            if route != "plan"
        )
        acceptance = LocalAcceptance(
            FakeHttp(),
            FakeCommands(outputs=[missing_logs]),
            environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"],
            ROOT,
        )
        with self.assertRaisesRegex(RuntimeError, "only HTTP 200"):
            acceptance.collect_ai_http_receipts(AI_ROUTE_PATHS)

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

    def test_task_verification_proof_lifecycle_records_redacted_receipt_fields(self) -> None:
        run_id = uid(2)
        http = ProofHttp(run_id)
        acceptance = LocalAcceptance(
            http, FakeCommands(), environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": run_id, "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"], ROOT,
        )
        acceptance.run_task_verification_proof()
        self.assertEqual(acceptance.proof_run_label, "seed")
        self.assertEqual(acceptance.proof_snapshot_id, uid(50))
        self.assertEqual(acceptance.reverified_snapshot_id, uid(51))
        self.assertEqual(acceptance.publication_state, "ACTIVE")
        self.assertIn(("POST", f"/project-runs/{run_id}/tasks/{SEED_TASK_KEY}/verify", {}), http.calls)

    def test_task_verification_proof_rejects_vacuous_evaluations(self) -> None:
        class VacuousProofHttp(ProofHttp):
            def _evaluations(self) -> list[dict[str, object]]:
                return []

        acceptance = LocalAcceptance(
            VacuousProofHttp(uid(2)), FakeCommands(), environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"], ROOT,
        )
        with self.assertRaisesRegex(RuntimeError, "evaluate every configured evidence rule"):
            acceptance.run_task_verification_proof()

    def test_restart_retention_restarts_api_and_backend_without_worker_reclaim(self) -> None:
        run_id = uid(2)
        http = RestartHttp(run_id)
        commands = FakeCommands()
        acceptance = LocalAcceptance(
            http, commands, environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": run_id, "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"], ROOT,
        )
        acceptance.proof_snapshot_id = uid(50)
        acceptance.publication_state = "ACTIVE"
        acceptance.run_restart_retention()
        flattened = [" ".join(command) for command in commands.commands]
        self.assertTrue(any(command.endswith(" stop api") for command in flattened))
        self.assertTrue(any("stop api workflow-worker" in command for command in flattened))
        self.assertFalse(any("docker kill --signal=KILL" in command for command in flattened))
        self.assertIn(uid(81), acceptance.resource_ids)

    def test_shell_entrypoint_has_exact_optional_reset_guard_and_parses(self) -> None:
        script = ROOT / "deploy/local-acceptance.sh"
        subprocess.run(["bash", "-n", str(script)], check=True)
        source = script.read_text()
        self.assertIn('"$reset" == "--reset"', source)
        self.assertIn('"--confirm=$project_name"', source)
        self.assertIn("--reset-performed", source)
        self.assertNotIn("local-reset.sh --confirm", source)
        self.assertIn("--profile", source)
        self.assertTrue((ROOT / "deploy/local-browser-gate.sh").is_file())

    def test_run_no_msw_browser_imports_sibling_module(self) -> None:
        source = (ROOT / "deploy/local_acceptance.py").read_text(encoding="utf-8")
        self.assertIn("from local_browser_gate import", source)
        self.assertNotIn("from deploy.local_browser_gate import", source)

    def test_phase2_run_order_places_browser_gate_before_proof(self) -> None:
        source = (ROOT / "deploy/local_acceptance.py").read_text(encoding="utf-8")
        browser_index = source.index("self.run_no_msw_browser()")
        rollback_index = source.index("self.run_rollback_browser()")
        project_runs_rollback_index = source.index("self.run_project_runs_rollback_browser()")
        proof_index = source.index("self.run_task_verification_proof()")
        self.assertLess(browser_index, proof_index)
        self.assertLess(browser_index, rollback_index)
        self.assertLess(rollback_index, project_runs_rollback_index)
        self.assertLess(project_runs_rollback_index, proof_index)

    def test_run_no_msw_browser_restarts_api_before_playwright(self) -> None:
        events: list[str] = []
        fake_gate = types.ModuleType("local_browser_gate")

        def fake_build_plan(**_kwargs):
            events.append("build_plan")
            return types.SimpleNamespace(playwright_env={})

        def fake_run_integrated(_plan, _env, *, between_spec_runs=None):
            events.append("run_integrated")
            if between_spec_runs is not None:
                events.append("between_spec_runs=callable")
            return "b" * 40

        def fake_read_env(_path):
            return {}

        fake_gate.BrowserGateError = RuntimeError
        fake_gate.build_plan = fake_build_plan
        fake_gate.read_env = fake_read_env
        fake_gate.run_integrated = fake_run_integrated

        class RecordingCommands(FakeCommands):
            def run(self, command, *, check=True):
                events.append(" ".join(command))
                return super().run(command, check=check)

        class BrowserAcceptance(LocalAcceptance):
            def wait_for_health_ready(self, timeout_seconds: int = 60) -> None:
                events.append(f"wait_for_health_ready:{timeout_seconds}")

        env_file = ROOT / "deploy/tests/.browser-gate-order.env"
        previous = os.environ.get("JAGALCHI_ACCEPTANCE_ENV_FILE")
        os.environ["JAGALCHI_ACCEPTANCE_ENV_FILE"] = str(env_file)
        env_file.write_text(
            "\n".join(
                [
                    "LOCAL_SEED_EMAIL=local@example.test",
                    "LOCAL_SEED_PASSWORD=super-secret-password",
                    f"PLATFORM_SOURCE_DIR={ROOT}",
                    f"API_SOURCE_DIR={ROOT}",
                    f"AI_SOURCE_DIR={ROOT}",
                ]
            ),
            encoding="utf-8",
        )
        try:
            with mock.patch.dict(sys.modules, {"local_browser_gate": fake_gate}):
                acceptance = BrowserAcceptance(
                    FakeHttp(),
                    RecordingCommands(),
                    environment(ROOT),
                    {
                        "schemaVersion": 1,
                        "userId": uid(1),
                        "projectRunId": uid(2),
                        "roadmapId": uid(3),
                    },
                    ["docker", "compose", "-p", "jagalchi-v1-local"],
                    ROOT,
                    repo_root=ROOT,
                    profile="phase2",
                )
                acceptance.run_no_msw_browser()
        finally:
            if previous is None:
                os.environ.pop("JAGALCHI_ACCEPTANCE_ENV_FILE", None)
            else:
                os.environ["JAGALCHI_ACCEPTANCE_ENV_FILE"] = previous
            env_file.unlink(missing_ok=True)

        restart_index = next(
            index for index, event in enumerate(events) if event.endswith(" restart api")
        )
        health_index = next(
            index for index, event in enumerate(events) if event.startswith("wait_for_health_ready:")
        )
        build_index = events.index("build_plan")
        playwright_index = events.index("run_integrated")
        self.assertLess(restart_index, health_index)
        self.assertLess(health_index, build_index)
        self.assertLess(build_index, playwright_index)


    def test_run_order_places_post_seed_readiness_before_fixture_path(self) -> None:
        source = (ROOT / "deploy/local_acceptance.py").read_text(encoding="utf-8")
        seed_index = source.index("self.login_and_verify_seed()")
        readiness_index = source.index("self.wait_for_post_seed_workflow_readiness()")
        fixture_index = source.index("self.run_fixture_path()")
        self.assertLess(seed_index, readiness_index)
        self.assertLess(readiness_index, fixture_index)

    def test_post_seed_readiness_wraps_readiness_error(self) -> None:
        class ReadyHttp(FakeHttp):
            def _response(self, method: str, target: str, body: object) -> HttpResponse:
                if (method, target) == ("GET", "/health/ready"):
                    return HttpResponse(200, {"status": "ready"})
                return super()._response(method, target, body)

        class FailingWorkerCommands(FakeCommands):
            def run(self, command, *, check=True):
                if any("health-check.js" in part for part in command):
                    raise subprocess.CalledProcessError(1, command)
                return super().run(command, check=check)

        clock = FakeClock()
        acceptance = LocalAcceptance(
            ReadyHttp(),
            FailingWorkerCommands(),
            environment(ROOT),
            {"schemaVersion": 1, "userId": uid(1), "projectRunId": uid(2), "roadmapId": uid(3)},
            ["docker", "compose", "-p", "jagalchi-v1-local"],
            ROOT,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        with self.assertRaisesRegex(RuntimeError, "workflow worker health-check.js failed"):
            acceptance.wait_for_post_seed_workflow_readiness(timeout_seconds=1)

if __name__ == "__main__":
    unittest.main()
