from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deploy.local_browser_gate import (
    BrowserGateError,
    browser_gate_env,
    build_plan,
    redact_output,
    run_integrated,
    run_standalone,
)


ROOT = Path(__file__).resolve().parents[2]


def uid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def seed() -> dict[str, str]:
    return {
        "schemaVersion": 1,
        "userId": uid(1),
        "projectRunId": uid(2),
        "roadmapId": uid(3),
    }


class BrowserGateTests(unittest.TestCase):
    def _platform_tree(self, root: Path, *, locked_head: str | None = None) -> tuple[Path, str]:
        platform = root / "platform"
        infra = root / "infra"
        (infra / "deploy").mkdir(parents=True)
        (platform / "apps/web/e2e-v1-local").mkdir(parents=True)
        (platform / "scripts").mkdir(parents=True)
        required = [
            "apps/web/e2e-v1-local/helpers.ts",
            "apps/web/e2e-v1-local/phase-one-entry.spec.ts",
            "apps/web/e2e-v1-local/phase-two-map-focus-proof.spec.ts",
            "apps/web/e2e-v1-local/phase-two-wave-b-entry.spec.ts",
            "apps/web/playwright.v1-local.config.ts",
            "scripts/test-v1-local-e2e.sh",
        ]
        for relative in required:
            target = platform / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(relative + "\n", encoding="utf-8")
        (platform / "scripts/test-v1-local-e2e.sh").chmod(0o755)
        (infra / "deploy/e2e-v1-local.manifest.json").write_text(
            (ROOT / "deploy/e2e-v1-local.manifest.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=platform, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=platform, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=platform, check=True)
        subprocess.run(["git", "add", "."], cwd=platform, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=platform, check=True)
        actual_head = subprocess.check_output(
            ["git", "-C", str(platform), "rev-parse", "HEAD"], text=True
        ).strip()
        lock_head = locked_head or actual_head
        (infra / "deploy/local-stack.lock.json").write_text(
            json.dumps({"revisions": {"platform": lock_head}}),
            encoding="utf-8",
        )
        return platform, actual_head

    def _env_file(self, root: Path, platform: Path) -> Path:
        env_file = root / "local.env"
        env_file.write_text(
            "\n".join(
                [
                    "LOCAL_SEED_EMAIL=local@example.test",
                    "LOCAL_SEED_PASSWORD=super-secret-password",
                    f"PLATFORM_SOURCE_DIR={platform}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return env_file

    def test_build_plan_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)
            self.assertEqual(Path(plan.standalone_command[0]).resolve(), (platform / "scripts/test-v1-local-e2e.sh").resolve())
            self.assertEqual([Path(part).resolve() if part.startswith("/") else part for part in plan.integrated_build_command], ["pnpm", "--dir", (platform / "apps/web").resolve(), "build"])
            self.assertIn("playwright.v1-local.config.ts", plan.integrated_playwright_command)
            self.assertEqual(plan.playwright_env["JAGALCHI_E2E_SEED_RUN_ID"], uid(2))

    def test_build_plan_phase2_limits_playwright_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
                profile="phase2",
            )
            joined = " ".join(plan.integrated_playwright_command)
            self.assertIn("e2e-v1-local/phase-two-map-focus-proof.spec.ts", joined)
            self.assertIn("e2e-v1-local/phase-two-wave-b-entry.spec.ts", joined)
            self.assertNotIn("apps/web/", joined)
            spec_paths = [
                argument
                for argument in plan.integrated_playwright_command
                if argument.endswith(".spec.ts")
            ]
            self.assertEqual(
                spec_paths,
                [
                    "e2e-v1-local/phase-two-map-focus-proof.spec.ts",
                    "e2e-v1-local/phase-two-wave-b-entry.spec.ts",
                ],
            )
            for argument in spec_paths:
                self.assertFalse(argument.startswith("apps/web/"), argument)
                self.assertTrue(
                    argument.startswith("e2e-v1-local/"),
                    f"expected web-relative spec path, got {argument!r}",
                )

    def test_build_plan_phase1_does_not_scope_phase2_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
                profile="phase1",
            )
            joined = " ".join(plan.integrated_playwright_command)
            self.assertNotIn("phase-two-map-focus-proof.spec.ts", joined)
            self.assertEqual(
                plan.integrated_playwright_command[-1],
                "playwright.v1-local.config.ts",
            )

    def test_browser_gate_env_disables_msw_mocking(self) -> None:
        env = browser_gate_env(
            {
                "LOCAL_SEED_EMAIL": "local@example.test",
                "LOCAL_SEED_PASSWORD": "super-secret-password",
            },
            seed(),
        )
        self.assertEqual(env["NEXT_PUBLIC_API_MOCKING"], "false")
        self.assertEqual(env["NEXT_PUBLIC_E2E_MOCKING"], "false")
        self.assertEqual(env["NEXT_PUBLIC_API_URL"], "/api")
        self.assertEqual(env["API_ORIGIN"], "http://127.0.0.1:8080")

    def test_build_plan_keeps_standalone_no_msw_harness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
                profile="phase2",
            )
            self.assertEqual(
                Path(plan.standalone_command[0]).name,
                "test-v1-local-e2e.sh",
            )

    def test_build_plan_phase2_required_specs_must_be_non_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            manifest_path = root / "infra" / "deploy/e2e-v1-local.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["phase2RequiredSpecs"] = []
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(BrowserGateError, "phase2RequiredSpecs is invalid"):
                build_plan(
                    repo_root=root / "infra",
                    env_file=env_file,
                    seed=seed(),
                    allow_dev_head=True,
                    profile="phase2",
                )

    def test_missing_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            (root / "infra" / "deploy/e2e-v1-local.manifest.json").unlink()
            with self.assertRaisesRegex(BrowserGateError, "manifest is missing"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)

    def test_manifest_requires_both_phase2_specs(self) -> None:
        manifest = json.loads((ROOT / "deploy/e2e-v1-local.manifest.json").read_text(encoding="utf-8"))
        map_focus = "apps/web/e2e-v1-local/phase-two-map-focus-proof.spec.ts"
        wave_b = "apps/web/e2e-v1-local/phase-two-wave-b-entry.spec.ts"
        self.assertIn(map_focus, manifest["requiredFiles"])
        self.assertIn(wave_b, manifest["requiredFiles"])
        self.assertEqual(manifest["phase2RequiredSpecs"], [map_focus, wave_b])

    def test_missing_phase2_map_focus_spec_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            (platform / "apps/web/e2e-v1-local/phase-two-map-focus-proof.spec.ts").unlink()
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "phase 2 browser spec is missing"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)

    def test_missing_phase2_wave_b_entry_spec_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            (platform / "apps/web/e2e-v1-local/phase-two-wave-b-entry.spec.ts").unlink()
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "phase 2 browser spec is missing"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)

    def test_missing_required_wave_b_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            wave_b = platform / "apps/web/e2e-v1-local/phase-two-wave-b-entry.spec.ts"
            wave_b.unlink()
            manifest_path = root / "infra" / "deploy/e2e-v1-local.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["phase2RequiredSpecs"] = [
                "apps/web/e2e-v1-local/phase-two-map-focus-proof.spec.ts",
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "platform inventory is incomplete"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)

    def test_build_plan_default_profile_does_not_scope_phase2_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
            )
            joined = " ".join(plan.integrated_playwright_command)
            self.assertNotIn("phase-two-map-focus-proof.spec.ts", joined)
            self.assertNotIn("phase-two-wave-b-entry.spec.ts", joined)
            self.assertEqual(
                plan.integrated_playwright_command[-1],
                "playwright.v1-local.config.ts",
            )

    def test_wrong_revision_without_dev_head_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root, locked_head="b" * 40)
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "unpinned platform checkout"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=False)

    def test_redacted_failure_output(self) -> None:
        env = {"LOCAL_SEED_PASSWORD": "super-secret-password"}
        message = "login failed password=super-secret-password"
        redacted = redact_output(message, env)
        self.assertNotIn("super-secret-password", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_run_integrated_propagates_nonzero_playwright(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)
            env = {"LOCAL_SEED_PASSWORD": "super-secret-password"}

            def fake_run(command, *, env, cwd=None):
                if command[0] == "pnpm" and "build" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 1, "", "super-secret-password leaked")

            with mock.patch("deploy.local_browser_gate.run_command", side_effect=fake_run):
                with self.assertRaisesRegex(BrowserGateError, "playwright failed"):
                    run_integrated(plan, env)

    def test_run_standalone_propagates_nonzero_harness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)
            env = {"LOCAL_SEED_PASSWORD": "super-secret-password"}

            def fake_run(command, *, env, cwd=None):
                return subprocess.CompletedProcess(command, 2, "", "harness failed")

            with mock.patch("deploy.local_browser_gate.run_command", side_effect=fake_run):
                with self.assertRaisesRegex(BrowserGateError, "harness failed"):
                    run_standalone(plan, env)

    def test_run_integrated_success_returns_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)

            def fake_run(command, *, env, cwd=None):
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch("deploy.local_browser_gate.run_command", side_effect=fake_run):
                revision = run_integrated(plan, {})
            self.assertEqual(len(revision), 40)


if __name__ == "__main__":
    unittest.main()
