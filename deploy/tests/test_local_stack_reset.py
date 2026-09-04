from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from deploy.local_stack_reset import (
    ResetError,
    allowlisted_container_names,
    disposal_decision,
    purge_stale_allowlisted_containers,
)


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = (ROOT / "compose.local.yml").resolve()


class LocalStackResetTest(unittest.TestCase):
    def test_allowlist_derived_from_compose_services(self) -> None:
        names = allowlisted_container_names(
            "jagalchi-v1-local",
            ["ai-db", "api", "workflow-worker"],
        )
        self.assertEqual(
            names,
            [
                "jagalchi-v1-local-ai-db-1",
                "jagalchi-v1-local-api-1",
                "jagalchi-v1-local-workflow-worker-1",
            ],
        )

    def test_disposal_accepts_label_drift_when_name_is_allowlisted(self) -> None:
        allowlist = {"jagalchi-v1-local-ai-db-1"}
        disposable, reason = disposal_decision(
            "jagalchi-v1-local-ai-db-1",
            {},
            allowlist=allowlist,
            project_name="jagalchi-v1-local",
            compose_file=COMPOSE_FILE,
        )
        self.assertTrue(disposable)
        self.assertIn("disposable", reason)

    def test_disposal_refuses_foreign_compose_project(self) -> None:
        allowlist = {"jagalchi-v1-local-ai-db-1"}
        disposable, reason = disposal_decision(
            "jagalchi-v1-local-ai-db-1",
            {"com.docker.compose.project": "other-project"},
            allowlist=allowlist,
            project_name="jagalchi-v1-local",
            compose_file=COMPOSE_FILE,
        )
        self.assertFalse(disposable)
        self.assertIn("foreign compose project", reason)

    def test_disposal_refuses_mismatched_compose_config_files(self) -> None:
        allowlist = {"jagalchi-v1-local-ai-db-1"}
        disposable, reason = disposal_decision(
            "jagalchi-v1-local-ai-db-1",
            {"com.docker.compose.project.config_files": "/tmp/other-compose.yml"},
            allowlist=allowlist,
            project_name="jagalchi-v1-local",
            compose_file=COMPOSE_FILE,
        )
        self.assertFalse(disposable)
        self.assertIn("config-files", reason)

    def test_purge_removes_only_allowlisted_stale_containers(self) -> None:
        removed: list[str] = []

        def fake_inspect(name: str):
            if name == "jagalchi-v1-local-ai-db-1":
                return {"Config": {"Labels": {"com.docker.compose.project": "jagalchi-v1-local"}}}
            return None

        def fake_remove(name: str) -> None:
            removed.append(name)

        purged = purge_stale_allowlisted_containers(
            repo_root=ROOT,
            env_file=ROOT / "deploy/local.env.example",
            inspect_container=fake_inspect,
            remove_container=fake_remove,
            discover_services=lambda *_args, **_kwargs: ["ai-db", "api"],
        )

        self.assertEqual(purged, ["jagalchi-v1-local-ai-db-1"])
        self.assertEqual(removed, ["jagalchi-v1-local-ai-db-1"])

    def test_purge_leaves_unrelated_containers_untouched(self) -> None:
        removed: list[str] = []

        purged = purge_stale_allowlisted_containers(
            repo_root=ROOT,
            env_file=ROOT / "deploy/local.env.example",
            inspect_container=lambda _name: None,
            remove_container=lambda name: removed.append(name),
            discover_services=lambda *_args, **_kwargs: ["ai-db"],
        )

        self.assertEqual(purged, [])
        self.assertEqual(removed, [])

    def test_purge_fails_closed_on_foreign_project_label(self) -> None:
        def fake_inspect(name: str):
            return {"Config": {"Labels": {"com.docker.compose.project": "evil-project"}}}

        with self.assertRaises(ResetError):
            purge_stale_allowlisted_containers(
                repo_root=ROOT,
                env_file=ROOT / "deploy/local.env.example",
                inspect_container=fake_inspect,
                remove_container=lambda _name: None,
                discover_services=lambda *_args, **_kwargs: ["ai-db"],
            )

    def test_local_reset_shell_invokes_stale_purge(self) -> None:
        script = (ROOT / "deploy/local-reset.sh").read_text(encoding="utf-8")
        self.assertIn("docker compose -p \"$project_name\"", script)
        self.assertIn("local_stack_reset.py", script)
        self.assertIn("purge-stale", script)

    def test_compose_services_matches_locked_stack(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "jagalchi-v1-local",
                "--env-file",
                str(ROOT / "deploy/local.env.example"),
                "-f",
                str(ROOT / "compose.local.yml"),
                "config",
                "--services",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        services = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(
            set(services),
            {
                "ai-db",
                "ai-migrate",
                "ai",
                "api-db",
                "api-migrate",
                "minio",
                "minio-init",
                "workflow-worker",
                "api",
            },
        )
        lock = json.loads((ROOT / "deploy/local-stack.lock.json").read_text(encoding="utf-8"))
        names = allowlisted_container_names(lock["project"], services)
        self.assertIn("jagalchi-v1-local-ai-db-1", names)
        self.assertEqual(len(names), len(services))


if __name__ == "__main__":
    unittest.main()
