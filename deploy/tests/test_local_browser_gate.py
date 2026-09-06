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
    integrated_playwright_commands,
    prepare_playwright_artifact_dirs,
    redact_output,
    run_full_web_e2e,
    run_integrated,
    run_rollback_smoke,
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
            "apps/web/e2e-v1-local/phase-two-closure.spec.ts",
            "apps/web/e2e-v1-local/phase-two-rollback.spec.ts",
            "apps/web/playwright.v1-local.config.ts",
            "scripts/test-v1-local-e2e.sh",
        ]
        manifest = json.loads(
            (ROOT / "deploy/e2e-v1-local.manifest.json").read_text(encoding="utf-8")
        )
        closure_markers = manifest["phase2ClosureSpec"]["requiredMarkers"]
        rollback_markers = manifest["rollbackSpec"]["requiredMarkers"]
        for relative in required:
            target = platform / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative.endswith("playwright.v1-local.config.ts"):
                content = (
                    "serviceWorkers: 'block'\n"
                    "NEXT_PUBLIC_API_MOCKING NEXT_PUBLIC_E2E_MOCKING\n"
                    "NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED: "
                    "process.env.NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED\n"
                    "NEXT_PUBLIC_PROJECT_RUNS_ENABLED: "
                    "process.env.NEXT_PUBLIC_PROJECT_RUNS_ENABLED\n"
                )
            elif relative.endswith("helpers.ts"):
                content = "expectNoServiceWorker navigator.serviceWorker\n"
            elif relative.endswith("phase-two-closure.spec.ts"):
                content = "\n".join(closure_markers) + "\n"
            elif relative.endswith("phase-two-rollback.spec.ts"):
                content = "\n".join(rollback_markers) + "\n"
            else:
                content = relative + "\n"
            target.write_text(content, encoding="utf-8")
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
                    "e2e-v1-local/phase-two-closure.spec.ts",
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

    def test_browser_gate_env_phase2_includes_project_runs_build_flags(self) -> None:
        env = browser_gate_env(
            {
                "LOCAL_SEED_EMAIL": "local@example.test",
                "LOCAL_SEED_PASSWORD": "super-secret-password",
            },
            seed(),
            profile="phase2",
        )
        self.assertEqual(env["NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED"], "true")
        self.assertEqual(env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"], "true")

    def test_browser_gate_env_phase1_omits_project_runs_flag(self) -> None:
        env = browser_gate_env(
            {
                "LOCAL_SEED_EMAIL": "local@example.test",
                "LOCAL_SEED_PASSWORD": "super-secret-password",
            },
            seed(),
            profile="phase1",
        )
        self.assertEqual(env["NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED"], "true")
        self.assertNotIn("NEXT_PUBLIC_PROJECT_RUNS_ENABLED", env)

    def test_run_integrated_phase2_build_env_includes_project_runs(self) -> None:
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
            captured: list[dict[str, str]] = []

            def fake_run(command, *, env, cwd=None):
                if command[0] == "pnpm" and command[-1] == "build":
                    captured.append(dict(env))
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch("deploy.local_browser_gate.run_command", side_effect=fake_run):
                run_integrated(
                    plan,
                    {"LOCAL_SEED_PASSWORD": "super-secret-password"},
                    between_spec_runs=lambda: None,
                )

            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED"], "true")
            self.assertEqual(captured[0]["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"], "true")

    def test_run_integrated_phase1_build_env_omits_project_runs(self) -> None:
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
            captured: list[dict[str, str]] = []

            def fake_run(command, *, env, cwd=None):
                if command[0] == "pnpm" and command[-1] == "build":
                    captured.append(dict(env))
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch("deploy.local_browser_gate.run_command", side_effect=fake_run):
                run_integrated(plan, {"LOCAL_SEED_PASSWORD": "super-secret-password"})

            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED"], "true")
            self.assertNotIn("NEXT_PUBLIC_PROJECT_RUNS_ENABLED", captured[0])

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
        map_focus = "e2e-v1-local/phase-two-map-focus-proof.spec.ts"
        wave_b = "e2e-v1-local/phase-two-wave-b-entry.spec.ts"
        self.assertIn(map_focus, manifest["requiredFiles"])
        self.assertIn(wave_b, manifest["requiredFiles"])
        closure = "e2e-v1-local/phase-two-closure.spec.ts"
        self.assertEqual(manifest["phase2RequiredSpecs"], [map_focus, wave_b, closure])
        self.assertEqual(manifest["phase2ClosureSpec"]["path"], closure)
        self.assertTrue(manifest["phase2ClosureSpec"]["requiredMarkers"])
        rollback = "e2e-v1-local/phase-two-rollback.spec.ts"
        self.assertIn(rollback, manifest["requiredFiles"])
        self.assertEqual(manifest["rollbackSpec"]["path"], rollback)
        self.assertTrue(manifest["rollbackSpec"]["requiredMarkers"])
        for spec in manifest["phase2RequiredSpecs"]:
            self.assertTrue(spec.startswith("e2e-v1-local/"), spec)
            self.assertFalse(spec.startswith("apps/web/"), spec)

    def test_missing_phase2_map_focus_spec_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            (platform / "apps/web/e2e-v1-local/phase-two-map-focus-proof.spec.ts").unlink()
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "phase2RequiredSpecs browser spec is missing"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)

    def test_missing_phase2_wave_b_entry_spec_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            (platform / "apps/web/e2e-v1-local/phase-two-wave-b-entry.spec.ts").unlink()
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "phase2RequiredSpecs browser spec is missing"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)
    def test_phase2_closure_requires_all_declared_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            closure = platform / "apps/web/e2e-v1-local/phase-two-closure.spec.ts"
            closure.write_text("phase2-closure:loading\n", encoding="utf-8")
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "missing required markers"):
                build_plan(
                    repo_root=root / "infra",
                    env_file=env_file,
                    seed=seed(),
                    allow_dev_head=True,
                    profile="phase2",
                )


    def test_missing_required_wave_b_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            wave_b = platform / "apps/web/e2e-v1-local/phase-two-wave-b-entry.spec.ts"
            wave_b.unlink()
            manifest_path = root / "infra" / "deploy/e2e-v1-local.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["phase2RequiredSpecs"] = [
                "e2e-v1-local/phase-two-map-focus-proof.spec.ts",
            ]
            manifest["v1LocalRequiredSpecs"] = [
                "e2e-v1-local/phase-one-entry.spec.ts",
                "e2e-v1-local/phase-two-map-focus-proof.spec.ts",
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

    def test_non_web_relative_phase2_spec_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            manifest_path = root / "infra" / "deploy/e2e-v1-local.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["phase2RequiredSpecs"] = [
                "apps/web/e2e-v1-local/phase-two-map-focus-proof.spec.ts",
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(BrowserGateError, "must be web-relative"):
                build_plan(repo_root=root / "infra", env_file=env_file, seed=seed(), allow_dev_head=True)

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

    def test_run_integrated_rejects_synthetic_canary_in_runner_output(self) -> None:
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
            plan.playwright_env["JAGALCHI_E2E_SYNTHETIC_CANARY"] = "safe-test-canary"

            def fake_run(command, *, env, cwd=None):
                if command[0] == "pnpm" and "build" in command:
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "safe-test-canary", "")

            with mock.patch("deploy.local_browser_gate.run_command", side_effect=fake_run):
                with self.assertRaisesRegex(BrowserGateError, "synthetic canary leaked"):
                    run_integrated(plan, {})

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

    def test_integrated_playwright_commands_split_phase2_specs(self) -> None:
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
            commands = integrated_playwright_commands(plan, between_spec_runs=lambda: None)
            self.assertEqual(len(commands), 3)
            self.assertIn("phase-two-map-focus-proof.spec.ts", commands[0][-1])
            self.assertIn("phase-two-wave-b-entry.spec.ts", commands[1][-1])
            self.assertIn("phase-two-closure.spec.ts", commands[2][-1])

    def test_run_integrated_phase2_invokes_between_spec_runs(self) -> None:
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
            between_calls: list[str] = []

            def fake_run(command, *, env, cwd=None):
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch("deploy.local_browser_gate.run_command", side_effect=fake_run):
                run_integrated(
                    plan,
                    {},
                    between_spec_runs=lambda: between_calls.append("reset"),
                )
            self.assertEqual(between_calls, ["reset", "reset"])

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


    def test_manifest_requires_v1_local_specs(self) -> None:
        manifest = json.loads((ROOT / "deploy/e2e-v1-local.manifest.json").read_text(encoding="utf-8"))
        expected = [
            "e2e-v1-local/phase-one-entry.spec.ts",
            "e2e-v1-local/phase-two-map-focus-proof.spec.ts",
            "e2e-v1-local/phase-two-wave-b-entry.spec.ts",
            "e2e-v1-local/phase-two-closure.spec.ts",
        ]
        self.assertEqual(manifest["v1LocalRequiredSpecs"], expected)

    def test_build_plan_full_web_lists_all_v1_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
                profile="full-web",
            )
            spec_paths = [
                argument
                for argument in plan.integrated_playwright_command
                if argument.endswith(".spec.ts")
            ]
            self.assertEqual(
                spec_paths,
                [
                    "e2e-v1-local/phase-one-entry.spec.ts",
                    "e2e-v1-local/phase-two-map-focus-proof.spec.ts",
                    "e2e-v1-local/phase-two-wave-b-entry.spec.ts",
                    "e2e-v1-local/phase-two-closure.spec.ts",
                ],
            )

    def test_integrated_playwright_commands_full_web_stays_single_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
                profile="full-web",
            )
            commands = integrated_playwright_commands(plan, between_spec_runs=lambda: None)
            self.assertEqual(len(commands), 1)
            self.assertEqual(len([part for part in commands[0] if part.endswith(".spec.ts")]), 4)

    def test_browser_gate_env_full_web_includes_project_runs(self) -> None:
        env = browser_gate_env(
            {
                "LOCAL_SEED_EMAIL": "local@example.test",
                "LOCAL_SEED_PASSWORD": "super-secret-password",
            },
            seed(),
            profile="full-web",
        )
        self.assertEqual(env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"], "true")
    def test_browser_gate_env_rollback_disables_project_runs(self) -> None:
        env = browser_gate_env(
            {
                "LOCAL_SEED_EMAIL": "local@example.test",
                "LOCAL_SEED_PASSWORD": "super-secret-password",
            },
            seed(),
            profile="rollback",
        )
        self.assertEqual(env["NEXT_PUBLIC_API_MOCKING"], "false")
        self.assertEqual(env["NEXT_PUBLIC_E2E_MOCKING"], "false")
        self.assertEqual(env["NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED"], "false")
        self.assertEqual(env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"], "false")
        self.assertEqual(env["CI"], "true")

    def test_browser_gate_env_project_runs_rollback_preserves_evidence(self) -> None:
        env = browser_gate_env(
            {
                "LOCAL_SEED_EMAIL": "local@example.test",
                "LOCAL_SEED_PASSWORD": "super-secret-password",
            },
            seed(),
            profile="project-runs-rollback",
        )
        self.assertEqual(env["NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED"], "true")
        self.assertEqual(env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"], "false")
        self.assertEqual(env["NEXT_PUBLIC_API_MOCKING"], "false")
        self.assertEqual(env["NEXT_PUBLIC_E2E_MOCKING"], "false")

    def test_build_plan_rollback_selects_dedicated_spec_without_grep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
                profile="rollback",
            )
            self.assertIsNone(plan.playwright_grep)
            self.assertEqual(plan.playwright_env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"], "false")
            self.assertNotIn("--grep", plan.integrated_playwright_command)
            self.assertEqual(
                [
                    argument
                    for argument in plan.integrated_playwright_command
                    if argument.endswith(".spec.ts")
                ],
                ["e2e-v1-local/phase-two-rollback.spec.ts"],
            )

    def test_build_plan_project_runs_rollback_selects_dedicated_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            plan = build_plan(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
                profile="project-runs-rollback",
            )
            self.assertEqual(plan.playwright_env["NEXT_PUBLIC_EVIDENCE_EXECUTION_ENABLED"], "true")
            self.assertEqual(plan.playwright_env["NEXT_PUBLIC_PROJECT_RUNS_ENABLED"], "false")
            self.assertEqual(
                [
                    argument
                    for argument in plan.integrated_playwright_command
                    if argument.endswith(".spec.ts")
                ],
                ["e2e-v1-local/phase-two-rollback.spec.ts"],
            )


    def test_unsupported_profile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            with self.assertRaisesRegex(BrowserGateError, "unsupported browser gate profile"):
                build_plan(
                    repo_root=root / "infra",
                    env_file=env_file,
                    seed=seed(),
                    allow_dev_head=True,
                    profile="not-a-profile",
                )

    def test_cli_validate_full_web_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "deploy/local_browser_gate.py"),
                    "validate",
                    "--env",
                    str(env_file),
                    "--repo-root",
                    str(root / "infra"),
                    "--seed-receipt",
                    json.dumps(seed()),
                    "--allow-dev-head",
                    "--profile",
                    "full-web",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("browser gate inventory: OK", completed.stdout)
            self.assertIn("profile=full-web", completed.stdout)


    def test_prepare_playwright_artifact_dirs_clears_stale_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            web_dir = platform / "apps/web"
            (web_dir / "test-results/trace.zip").parent.mkdir(parents=True)
            (web_dir / "test-results/trace.zip").write_text("stale", encoding="utf-8")
            (web_dir / "e2e-v1-local/.auth/seed-user.json").parent.mkdir(parents=True)
            (web_dir / "e2e-v1-local/.auth/seed-user.json").write_text("{}", encoding="utf-8")

            cleared = prepare_playwright_artifact_dirs(platform)

            self.assertEqual(sorted(cleared), ["e2e-v1-local/.auth", "test-results"])
            self.assertFalse((web_dir / "test-results").exists())
            self.assertFalse((web_dir / "e2e-v1-local/.auth").exists())

    def test_prepare_playwright_artifact_dirs_noop_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            self.assertEqual(prepare_playwright_artifact_dirs(platform), [])

    @mock.patch("deploy.local_browser_gate.run_integrated", return_value="a" * 40)
    @mock.patch("deploy.local_browser_gate.prepare_playwright_artifact_dirs", return_value=["test-results"])
    def test_run_full_web_e2e_prepares_then_runs_single_integrated_pass(
        self,
        prepare_mock: mock.MagicMock,
        integrated_mock: mock.MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            revision = run_full_web_e2e(
                repo_root=root / "infra",
                env_file=env_file,
                seed=seed(),
                allow_dev_head=True,
            )
            self.assertEqual(revision, "a" * 40)
            prepare_mock.assert_called_once_with(platform.resolve())
            integrated_mock.assert_called_once()
            plan = integrated_mock.call_args.args[0]
            self.assertEqual(plan.profile, "full-web")
            self.assertIsNone(integrated_mock.call_args.kwargs.get("between_spec_runs"))

    def test_cli_run_full_web_e2e_fails_closed_without_seed_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            platform, _ = self._platform_tree(root)
            env_file = self._env_file(root, platform)
            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "deploy/local_browser_gate.py"),
                    "run-full-web-e2e",
                    "--env",
                    str(env_file),
                    "--repo-root",
                    str(root / "infra"),
                    "--seed-receipt",
                    json.dumps({"schemaVersion": 1}),
                    "--allow-dev-head",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("browser gate: FAILED", completed.stderr)

    def test_full_web_runner_script_orchestrates_clean_seed_gate(self) -> None:
        script = (ROOT / "deploy/local-full-web-e2e.sh").read_text(encoding="utf-8")
        self.assertIn("local-doctor.sh", script)
        self.assertIn("--reset", script)
        self.assertIn("local-reset.sh", script)
        self.assertIn("local-seed.sh", script)
        self.assertIn("run-full-web-e2e", script)
        self.assertNotIn("run-integrated", script)

if __name__ == "__main__":
    unittest.main()
