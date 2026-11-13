# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Host-only production-interface tests; no SDK, GPU, Docker, or network needed."""

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_transition as rt


def git(checkout: Path, *arguments: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=checkout,
        env=rt.clean_environment(),
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def tool_source(root: Path) -> tuple[Path, str]:
    checkout = root / "tool-source"
    checkout.mkdir()
    for name in (
        "build_tools/runtime_transition.py",
        "build_tools/runtime_transition_report.py",
        "build_tools/runtime_transition_snapshot.py",
        "build_tools/runtime_transition/ctest_driver.py",
        "build_tools/runtime_transition/loader_audit.c",
        "build_tools/github_actions/detect_failed_logs.py",
        "build_tools/github_actions/github_actions_api.py",
    ):
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rt.SCRIPT_DIR.parent / name, target)
    (checkout / "unrelated.txt").write_text("Outside the orchestration snapshot\n")
    git(checkout, "init")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Orchestration fixture")
    return checkout, git(checkout, "rev-parse", "HEAD")


class TransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = argparse.Namespace(
            work_dir=self.root / "run",
            therock_repository="ROCm/TheRock",
            therock_ref="a" * 40,
            runtime_ref="b" * 40,
            container_image="sha256:" + "c" * 64,
            gpu="gfx942",
            gpu_family="gfx94X-dcgpu",
            jobs=2,
            evidence_url=None,
            therock_checkout=None,
            image_metadata=None,
        )
        self.args.image_metadata = self.root / "image.json"
        rt.write_json(
            self.args.image_metadata,
            {
                "Id": self.args.container_image,
                "Labels": {
                    "org.therock.runtime-transition." + key: value
                    for key, value in (
                        ("runtime-ref", "b" * 40),
                        ("cargo-lock-sha256", "e" * 64),
                        ("rust-toolchain", "1.98.0"),
                    )
                },
            },
        )
        self.plan = rt.Plan(
            "d" * 64,
            [rt.Suite("hip", "hip", "ctest", "dist/rocm/share/hip/catch_tests")],
            {},
        )
        self.environment = {
            "os": "ubuntu-24.04",
            "kernel": "6.8.0",
            "driver": "A123",
            "gpu": "gfx942-count1",
            "container": self.args.container_image,
        }
        self.run = rt.Transition(self.args, self.plan)
        rt.initialize_missing(self.run)

    def test_environment_cannot_inherit_provider_or_test_selection(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LD_PRELOAD": "bad.so",
                "LD_LIBRARY_PATH": "/opt/rocm/lib",
                "PATH": "/opt/rocm/bin",
                "GTEST_FILTER": "one",
                "EXTERNAL_REPO_CONFIG": "bad",
                "CMAKE_PREFIX_PATH": "/opt/rocm",
            },
            clear=True,
        ):
            environment = rt.clean_environment(self.root / "sdk")
        self.assertEqual(environment["ROCM_PATH"], str(self.root / "sdk"))
        self.assertNotIn("LD_PRELOAD", environment)
        self.assertNotIn("GTEST_FILTER", environment)
        self.assertNotIn("EXTERNAL_REPO_CONFIG", environment)
        self.assertNotIn("/opt/rocm", ":".join(environment.values()))

    def test_local_manifest_has_no_invented_ci_links(self) -> None:
        self.run.observe(
            self.run.jobs["prerequisites"],
            "baseline",
            rt.CommandResult("infrastructure_failure", False),
        )
        data = json.loads((self.run.work / "manifest.json").read_text())
        self.assertEqual(data["run"]["event"], "local")
        self.assertIsNone(data["run"]["url"])
        observation = data["jobs"][0]["observations"]["baseline"]
        self.assertIsNone(observation["evidence_url"])
        self.assertEqual(observation["evidence_locator"], "diagnostics.tar.gz")
        self.assertNotIn("/actions/runs/", json.dumps(data))

    def test_missing_manifest_finish_scaffold_is_incomplete(self) -> None:
        data = json.loads((self.run.work / "manifest.json").read_text())
        self.assertTrue(all(not job["observations"] for job in data["jobs"]))
        self.assertEqual(len(data["jobs"][-1]["expected_tests"]), 3)
        self.assertEqual(
            data["profiles"]["baseline"]["configuration"]["hip_flag"], "ON"
        )
        self.assertEqual(
            data["profiles"]["candidate"]["configuration"]["runtime_flag"], "ON"
        )

    def test_runner_timeout_kills_process_group_and_preserves_log(self) -> None:
        process = MagicMock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired("cmake", 1), 9]
        with patch.object(rt.subprocess, "Popen", return_value=process), patch.object(
            rt.os, "killpg"
        ) as kill:
            result = self.run.runner.run(["cmake"], cwd=self.root, env={}, timeout=1)
        self.assertEqual(result, rt.CommandResult("timed_out", True))
        kill.assert_called_once_with(123, rt.signal.SIGKILL)
        self.assertTrue((self.run.work / "diagnostics/command-0001.json").is_file())

    def test_runner_spawn_failure_is_infrastructure(self) -> None:
        with patch.object(
            rt.subprocess, "Popen", side_effect=FileNotFoundError("missing executable")
        ):
            result = self.run.runner.run(["missing"], cwd=self.root, env={}, timeout=1)
        self.assertEqual(result, rt.CommandResult("infrastructure_failure", False))
        self.assertIn(
            "missing executable",
            (self.run.work / "diagnostics/command-0001.stderr").read_text(),
        )

    def test_interrupt_retains_cancelled_observation(self) -> None:
        other = rt.Transition(
            argparse.Namespace(
                **{**vars(self.args), "work_dir": self.root / "cancelled"}
            ),
            self.plan,
        )

        def interrupted(command: list[str], **kwargs: object) -> rt.CommandResult:
            raise KeyboardInterrupt

        with patch.object(
            rt, "validate_image_provenance", return_value={"fixture": True}
        ), patch.object(
            rt, "hardware_identity", return_value=self.environment
        ), patch.object(
            other.runner, "run", side_effect=interrupted
        ):
            self.assertEqual(other.run(), 130)
        data = json.loads((other.work / "manifest.json").read_text())
        sources = next(j for j in data["jobs"] if j["id"] == "sources")
        self.assertEqual(sources["observations"]["baseline"]["status"], "cancelled")
        self.assertNotIn("candidate", sources["observations"])
        self.assertTrue((other.work / "diagnostics.tar.gz").is_file())

    def test_baseline_fetch_failure_does_not_prevent_candidate_attempt(self) -> None:
        other = rt.Transition(
            argparse.Namespace(**{**vars(self.args), "work_dir": self.root / "failed"}),
            self.plan,
        )
        calls = []

        def failure(command: list[str], **kwargs: object) -> rt.CommandResult:
            calls.append(command)
            return rt.CommandResult("failure", True)

        with patch.object(
            rt, "validate_image_provenance", return_value={"fixture": True}
        ), patch.object(
            rt, "hardware_identity", return_value=self.environment
        ), patch.object(
            other.runner, "run", side_effect=failure
        ):
            self.assertEqual(other.run(), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("/baseline/source", calls[0][-1])
        self.assertIn("/candidate/source", calls[1][-1])
        self.assertEqual(
            other.jobs["sources"].observations["candidate"]["status"],
            "infrastructure_failure",
        )
        self.assertEqual(other.jobs["dist"].observations, {})

    def test_plan_rejects_arbitrary_commands_and_empty_selection(self) -> None:
        path = self.root / "plan.json"
        for suites in (
            [],
            [{"id": "fake", "component": "hip", "kind": "shell", "path": "true"}],
        ):
            path.write_text(
                json.dumps({"schema_version": 1, "cmake": {}, "suites": suites})
            )
            with self.assertRaises(ValueError):
                rt.load_plan(path)

    def test_ctest_inventory_empty_and_duplicates_fail(self) -> None:
        path = self.root / "inventory.json"
        for tests in ([], [{"name": "same"}, {"name": "same"}]):
            path.write_text(json.dumps({"kind": "ctestInfo", "tests": tests}))
            with self.assertRaises(rt.PrerequisiteError):
                rt.ctest_inventory(path, "hip")

    def test_gtest_inventory_preserves_parameter_identity(self) -> None:
        path = self.root / "list.txt"
        path.write_text(
            "TypedFixture/0.  # TypeParam = bool\n  Works/0 # GetParam() = 3\n  DISABLED_Later\n"
        )
        self.assertEqual(
            rt.gtest_inventory(path),
            [{"classname": "TypedFixture/0", "name": "Works/0"}],
        )

    def test_junit_redacts_diagnostics_preserves_outcomes(self) -> None:
        raw = self.root / "raw.xml"
        output = self.root / "public.junit"
        raw.write_text(
            '<testsuite tests="2" failures="1"><testcase classname="private/path" name="A"><failure message="/private/path">secret</failure><system-out>secret</system-out></testcase><testcase name="B"/></testsuite>'
        )
        rt.normalize_junit(raw, output, rt.Suite("hip", "hip", "ctest"))
        text = output.read_text()
        self.assertNotIn("secret", text)
        self.assertNotIn("private/path", text)
        self.assertEqual(len(ET.parse(output).findall(".//failure")), 1)
        self.assertEqual(ET.parse(output).find(".//testcase").get("classname"), "hip")

    def test_malformed_and_empty_junit_fail(self) -> None:
        raw = self.root / "raw.xml"
        for content in ("<bad", '<testsuite tests="0"/>'):
            raw.write_text(content)
            with self.assertRaises((ET.ParseError, rt.PrerequisiteError)):
                rt.normalize_junit(raw, self.root / "public.junit", self.plan.suites[0])

    def fake_ctest(self, command: list[str], **kwargs: object) -> rt.CommandResult:
        if "--show-only=json-v1" in command:
            kwargs["output"].write_text(
                json.dumps({"kind": "ctestInfo", "tests": [{"name": "Allocate"}]})
            )
        else:
            raw = Path(command[command.index("--output-junit") + 1])
            raw.write_text(
                '<testsuite tests="1" failures="0"><testcase name="Allocate"/></testsuite>'
            )
        return rt.CommandResult("success", True)

    def test_actual_ctest_inventory_junit_manifest_binding(self) -> None:
        with patch.object(self.run.runner, "run", side_effect=self.fake_ctest):
            self.run.test_suite(
                "baseline",
                self.root,
                self.root,
                rt.clean_environment(),
                self.plan.suites[0],
            )
        job = self.run.jobs["test-hip"]
        self.assertEqual(job.expected_tests, [{"classname": "hip", "name": "Allocate"}])
        path = self.run.work / job.observations["baseline"]["junit"][0]["path"]
        self.assertEqual(
            job.observations["baseline"]["junit"][0]["sha256"], rt.digest(path)
        )
        self.assertNotIn("candidate", job.observations)

    def test_successful_ctest_without_junit_stays_incomplete(self) -> None:
        def missing(command: list[str], **kwargs: object) -> rt.CommandResult:
            if "--show-only=json-v1" in command:
                return self.fake_ctest(command, **kwargs)
            return rt.CommandResult("success", True)

        with patch.object(self.run.runner, "run", side_effect=missing):
            self.run.test_suite(
                "candidate", self.root, self.root, {}, self.plan.suites[0]
            )
        self.assertEqual(
            self.run.jobs["test-hip"].observations["candidate"]["junit"], []
        )

    def test_paired_different_inventory_refuses_candidate_coverage(self) -> None:
        with patch.object(self.run.runner, "run", side_effect=self.fake_ctest):
            self.run.test_suite(
                "baseline", self.root, self.root, {}, self.plan.suites[0]
            )

        def mismatch(command: list[str], **kwargs: object) -> rt.CommandResult:
            kwargs["output"].write_text(
                json.dumps({"kind": "ctestInfo", "tests": [{"name": "Different"}]})
            )
            return rt.CommandResult("success", True)

        with patch.object(
            self.run.runner, "run", side_effect=mismatch
        ), self.assertRaisesRegex(rt.PrerequisiteError, "inventories differ"):
            self.run.test_suite(
                "candidate", self.root, self.root, {}, self.plan.suites[0]
            )
        self.assertNotIn("candidate", self.run.jobs["test-hip"].observations)

    def test_provider_digest_mismatch_fails_before_any_runtime_command(self) -> None:
        build = self.root / "build"
        library = build / "dist/rocm/lib/libhsa-runtime64.so.1"
        library.parent.mkdir(parents=True)
        library.write_bytes(b"wrong-provider")
        stage = build / "core/rust-hsa/stage/lib/libhsa-runtime64.so.1"
        stage.parent.mkdir(parents=True)
        stage.write_bytes(b"candidate")
        with patch.object(self.run.runner, "run") as runner, self.assertRaisesRegex(
            rt.PrerequisiteError, "selected provider"
        ):
            self.run.provider("candidate", self.root, build, {})
        runner.assert_not_called()

    def test_hsa_init_failure_records_real_api_failure_and_blocks_followups(
        self,
    ) -> None:
        library = MagicMock()
        library.hsa_init.return_value = 0x1008
        output = self.root / "foundation.xml"
        with patch.object(rt.ctypes, "CDLL", return_value=library):
            self.assertEqual(rt.foundation(output), 1)
        root = ET.parse(output)
        self.assertEqual(
            root.find('.//testcase[@name="init"]/failure').get("message"),
            "HSA status 0x1008",
        )
        self.assertEqual(len(root.findall(".//skipped")), 2)
        library.hsa_iterate_agents.assert_not_called()
        library.hsa_shut_down.assert_not_called()

    def test_hsa_success_requires_gpu_and_shutdown(self) -> None:
        library = MagicMock()
        library.hsa_init.return_value = 0
        library.hsa_iterate_agents.return_value = 0
        library.hsa_shut_down.return_value = 0
        output = self.root / "foundation.xml"
        with patch.object(rt.ctypes, "CDLL", return_value=library):
            self.assertEqual(rt.foundation(output), 1)
        self.assertIsNotNone(
            ET.parse(output).find('.//testcase[@name="enumerate-gpu"]/failure')
        )
        library.hsa_shut_down.assert_called_once()

    def test_build_triage_uses_end_exit_not_error_keywords(self) -> None:
        build = self.root / "build"
        (build / "logs").mkdir(parents=True)
        rt.write_json(
            build / "therock_consumer_graph.json",
            {"hip-clr": {"consumers": []}, "rocblas": {"consumers": []}},
        )
        (build / "logs/hip-clr_build.log").write_text("END\t123\t1\t2\n")
        (build / "logs/rocblas_build.log").write_text(
            "error: text from expected negative test\nEND\t123\t1\t0\n"
        )
        stamp = build / "math-libs/rocblas/stamp/build.stamp"
        stamp.parent.mkdir(parents=True)
        stamp.touch()
        (stamp.parent.parent / "build_init.cmake").write_text(
            'set(THEROCK_SUBPROJECT_TARGET "rocblas")\nset(THEROCK_BUILD_STAMP_FILE "'
            + str(stamp)
            + '")\n'
        )
        triage = rt.build_triage(build)
        self.assertEqual(
            triage["failures"],
            [
                {
                    "subproject": "hip-clr",
                    "phase": "build",
                    "exit_code": 2,
                    "log": "logs/hip-clr_build.log",
                }
            ],
        )
        self.assertEqual(triage["completed_phases"], {"rocblas": ["build"]})

    def test_build_jobs_have_distinct_phases_and_block_consumers(self) -> None:
        graph = {
            "rocr-runtime": {"consumers": ["hip-clr"]},
            "hip-clr": {"consumers": []},
        }
        self.run.record_build_triage(
            "baseline",
            {
                "consumer_graph": graph,
                "failures": [{"subproject": "hip-clr", "phase": "build"}],
                "completed_phases": {
                    "rocr-runtime": ["configure", "build", "stage"],
                    "hip-clr": ["configure"],
                },
            },
        )
        job = self.run.jobs["build-hip-clr-build"]
        self.assertEqual(job.observations["baseline"]["status"], "failure")
        self.assertIn(
            "build-hsa-provider-install", self.run.jobs["build-hip-clr-configure"].needs
        )
        self.assertEqual(self.run.jobs["build-hip-clr-install"].observations, {})
        self.assertIn("build-hip-clr-install", self.run.jobs["dist"].needs)

    def test_provider_phase_requires_both_candidate_hsa_and_thunk(self) -> None:
        graph = {
            "rust-hsa": {"consumers": ["hip-clr"]},
            "hsakmt": {"consumers": ["hip-clr"]},
            "hip-clr": {"consumers": []},
        }
        triage = {
            "consumer_graph": graph,
            "failures": [{"subproject": "hsakmt", "phase": "build"}],
            "completed_phases": {
                "rust-hsa": ["configure", "build", "stage"],
                "hsakmt": ["configure"],
            },
        }
        self.run.record_build_triage("candidate", triage)
        self.assertEqual(
            self.run.jobs["build-hsa-provider-configure"].observations["candidate"][
                "status"
            ],
            "success",
        )
        self.assertEqual(
            self.run.jobs["build-hsa-provider-build"].observations["candidate"][
                "status"
            ],
            "failure",
        )
        self.assertEqual(self.run.jobs["build-hsa-provider-install"].observations, {})
        self.assertNotIn("build-hsakmt-build", self.run.jobs)
        self.assertNotIn(
            "build-hsa-provider-install",
            self.run.jobs["build-hsa-provider-configure"].needs,
        )

    def test_unknown_junit_result_cannot_be_sanitized_to_success(self) -> None:
        raw = self.root / "raw.xml"
        output = self.root / "selected.junit"
        raw.write_text(
            '<testsuite tests="1"><testcase name="A"><flakyFailure message="private">secret</flakyFailure></testcase></testsuite>'
        )
        rt.normalize_junit(raw, output, self.plan.suites[0])
        self.assertIsNotNone(ET.parse(output).find(".//flakyFailure"))
        self.assertNotIn("secret", output.read_text())

    def test_clean_local_commit_bundle_without_push_and_dirty_refusal(self) -> None:
        checkout = self.root / "source"
        checkout.mkdir()
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }

        def git(*arguments: str) -> str:
            return subprocess.check_output(
                ["git", *arguments],
                cwd=checkout,
                env=environment,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()

        git("init")
        (checkout / ".gitignore").write_text("build/\n")
        git("add", ".gitignore")
        git("commit", "-m", "fixture")
        commit = git("rev-parse", "HEAD")
        (checkout / "build").mkdir()
        (checkout / "build/cache").write_text("ignored")
        rt.validate_checkout(checkout, commit)
        bundle = self.root / "source.bundle"
        git("bundle", "create", str(bundle), "HEAD")
        rt.validate_checkout(bundle, commit)
        (checkout / "untracked").write_text("dirty")
        with self.assertRaisesRegex(rt.PrerequisiteError, "no tracked or untracked"):
            rt.validate_checkout(checkout, commit)
        with self.assertRaisesRegex(rt.PrerequisiteError, "differs"):
            rt.validate_checkout(checkout, "f" * 40)

    def test_detached_source_snapshot_from_actual_shallow_worktree_objects(
        self,
    ) -> None:
        original = rt.SCRIPT_DIR.parent
        common = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=original,
                text=True,
            ).strip()
        )
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=original, text=True
        ).strip()
        self.args.source_git_dir = common
        self.args.therock_ref = commit
        self.run = rt.Transition(self.args, self.plan)
        snapshots = {
            name: (common / name).read_bytes()
            for name in ("HEAD", "config", "shallow")
            if (common / name).is_file()
        }
        original_run = self.run.runner.run

        def stop_external_fetch(
            command: list[str], **kwargs: object
        ) -> rt.CommandResult:
            if any("fetch_sources.py" in argument for argument in command):
                return rt.CommandResult("infrastructure_failure", False)
            self.assertNotEqual(command[:2], ["git", "fetch"])
            return original_run(command, **kwargs)

        with patch.object(self.run.runner, "run", side_effect=stop_external_fetch):
            self.assertIsNone(
                self.run.prepare_source("baseline", rt.clean_environment())
            )
        source = self.run.work / "baseline/source"
        actual = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        self.assertEqual(actual, commit)
        self.assertEqual(
            (source / ".git/objects/info/alternates").read_text(),
            str(common / "objects") + "\n",
        )
        self.assertFalse((source / "rocm-systems/.git").exists())
        for name, value in snapshots.items():
            self.assertEqual((common / name).read_bytes(), value)

    def test_recursive_object_stores_are_bounded_and_cycle_safe(self) -> None:
        common = self.root / "common.git"
        first = common / "objects"
        second = self.root / "alternate-objects"
        (first / "info").mkdir(parents=True)
        (second / "info").mkdir(parents=True)
        (first / "info/alternates").write_text(str(second) + "\n")
        (second / "info/alternates").write_text(str(first) + "\n")
        self.assertEqual(rt.git_object_stores(common), [first, second])

    def test_local_launcher_pins_inspected_id_and_omits_absent_evidence(self) -> None:
        self.args.therock_checkout, self.args.therock_ref = tool_source(self.root)
        self.args.work_dir = self.root / "launch"
        self.args.plan = rt.SUPPORT_DIR / "bringup.json"
        metadata = {
            "Id": self.args.container_image,
            "RepoDigests": [],
            "Config": {"Labels": {}},
        }
        check_output = subprocess.check_output

        def inspect(command: list[str], **kwargs: object) -> str:
            if command[0] == "docker":
                return json.dumps(metadata)
            return check_output(command, **kwargs)

        with patch.object(
            rt.subprocess, "check_output", side_effect=inspect
        ), patch.object(rt.subprocess, "call", return_value=1) as launch:
            self.assertEqual(rt.launch_container(self.args, self.plan), 1)
        command = launch.call_args.args[0]
        self.assertTrue(all(isinstance(item, str) for item in command))
        self.assertNotIn("--evidence-url", command)
        self.assertEqual(
            command[command.index("--security-opt") + 1], "seccomp=unconfined"
        )
        self.assertIn("memlock=-1:-1", command)
        self.assertIn("nofile=1048576:1048576", command)
        self.assertNotIn("--privileged", command)
        metadata_path = self.args.work_dir.parent / (
            "." + self.args.work_dir.name + "-image.json"
        )
        self.assertEqual(
            json.loads(metadata_path.read_text())["RuntimeOptions"],
            rt.GPU_CONTAINER_OPTIONS,
        )
        self.assertIn(self.args.container_image, command)
        self.assertNotIn("/opt/rocm", " ".join(command))
        snapshot = (
            self.args.work_dir.parent / f".{self.args.work_dir.name}-orchestrator"
        )
        self.assertIn(f"type=bind,src={snapshot},dst=/orchestrator,readonly", command)
        self.assertIn(
            f"type=bind,src={snapshot},dst=/runs/{snapshot.name},readonly", command
        )
        self.assertNotIn(f"src={rt.SCRIPT_DIR.parent},", " ".join(command))
        self.assertEqual(
            rt.verify_orchestration_snapshot(self.args.work_dir, self.args.therock_ref),
            snapshot,
        )
        common = Path(git(self.args.therock_checkout, "rev-parse", "--git-common-dir"))
        common = (self.args.therock_checkout / common).resolve()
        self.assertIn(f"type=bind,src={common},dst={common},readonly", command)
        self.assertIn(
            f"type=bind,src={common / 'objects'},dst={common / 'objects'},readonly",
            command,
        )
        self.assertTrue((self.args.work_dir / "manifest.json").is_file())

    def test_registry_digest_mismatch_prevents_container_execution(self) -> None:
        self.args.therock_checkout, self.args.therock_ref = tool_source(self.root)
        self.args.work_dir = self.root / "bad-image"
        self.args.plan = rt.SUPPORT_DIR / "bringup.json"
        self.args.container_image = "registry/image@sha256:" + "c" * 64
        metadata = {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": ["other/image@sha256:" + "b" * 64],
        }
        check_output = subprocess.check_output
        run = subprocess.run

        def inspect(command: list[str], **kwargs: object) -> str:
            if command[0] == "docker":
                return json.dumps(metadata)
            return check_output(command, **kwargs)

        def pull(command: list[str], **kwargs: object) -> object:
            if command[:2] == ["docker", "pull"]:
                return subprocess.CompletedProcess(command, 0)
            return run(command, **kwargs)

        with patch.object(rt.subprocess, "run", side_effect=pull), patch.object(
            rt.subprocess, "check_output", side_effect=inspect
        ), patch.object(rt.subprocess, "call") as launch, self.assertRaisesRegex(
            ValueError, "did not match"
        ):
            rt.launch_container(self.args, self.plan)
        launch.assert_not_called()

    def test_snapshot_pins_exact_git_tools_after_original_edits(self) -> None:
        source, commit = tool_source(self.root)
        self.args.therock_checkout = source
        self.args.therock_ref = commit
        self.args.work_dir = self.root / "snapshot-run"
        donor = {
            str(path.relative_to(source)): path.read_bytes()
            for path in (source / ".git").rglob("*")
            if path.is_file()
        }
        snapshot = rt.create_orchestration_snapshot(self.args)
        self.assertFalse(self.args.work_dir.exists())
        self.assertFalse((snapshot / "unrelated.txt").exists())
        self.assertFalse((snapshot / ".git").exists())
        self.assertEqual(
            donor,
            {
                str(path.relative_to(source)): path.read_bytes()
                for path in (source / ".git").rglob("*")
                if path.is_file()
            },
        )
        expected = {
            name: (snapshot / name).read_bytes()
            for name in rt.snapshot.ORCHESTRATION_HELPERS
        }
        for name in expected:
            self.assertEqual(expected[name], (source / name).read_bytes())
            (source / name).write_text("raise SystemExit(99)\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "Edit original tools after snapshot")
        self.assertNotEqual(git(source, "rev-parse", "HEAD"), commit)
        self.assertEqual(
            rt.verify_orchestration_snapshot(self.args.work_dir, commit), snapshot
        )
        for name, content in expected.items():
            self.assertEqual((snapshot / name).read_bytes(), content)
        output = subprocess.check_output(
            [
                sys.executable,
                str(snapshot / "build_tools/runtime_transition.py"),
                "--help",
            ],
            env=rt.clean_environment(),
            text=True,
        )
        self.assertIn("container", output)
        metadata = json.loads((snapshot / "snapshot.json").read_text())
        self.assertEqual(metadata["source_sha"], commit)
        self.assertEqual(
            metadata["archive_sha256"], rt.digest(snapshot / "build-tools.tar")
        )
        with self.assertRaises(FileExistsError):
            # Reuse cannot silently repin a run after the original changes.
            self.args.therock_checkout = None
            rt.create_orchestration_snapshot(self.args)

    def test_current_checkout_snapshot_uses_requested_commit_not_worktree(self) -> None:
        source, commit = tool_source(self.root)
        self.args.therock_ref = commit
        runner = source / "build_tools/runtime_transition.py"
        expected = runner.read_bytes()
        runner.write_text("raise SystemExit(99)\n")
        git(source, "add", ".")
        git(source, "commit", "-m", "Different checkout HEAD")
        with patch.object(rt, "SCRIPT_DIR", source / "build_tools"):
            snapshot = rt.create_orchestration_snapshot(self.args)
        self.assertEqual(
            (snapshot / "build_tools/runtime_transition.py").read_bytes(), expected
        )

    def test_finish_and_later_finish_execute_retained_pinned_reporter(self) -> None:
        source, commit = tool_source(self.root)
        self.args.therock_checkout = source
        self.args.therock_ref = commit
        self.args.work_dir = self.root / "finish-run"
        snapshot = rt.create_orchestration_snapshot(self.args)
        (source / "build_tools/runtime_transition_report.py").write_text(
            "raise SystemExit(99)\n"
        )
        transition = rt.Transition(self.args, self.plan)
        with patch.object(rt, "SCRIPT_DIR", source / "build_tools"):
            self.assertEqual(rt.finish(transition), 1)
            report = json.loads((transition.work / "reports/report.json").read_text())
            self.assertEqual(
                report["profiles"]["baseline"]["sources"]["therock"], commit
            )
            self.assertEqual(rt.finish(rt.Transition(self.args, self.plan)), 1)
        rt.verify_orchestration_snapshot(transition.work, commit)
        self.assertFalse(list(snapshot.rglob("*.pyc")))

    def test_report_preparation_executes_the_same_pinned_reporter(self) -> None:
        source, commit = tool_source(self.root)
        self.args.therock_checkout = source
        self.args.therock_ref = commit
        rt.create_orchestration_snapshot(self.args)
        (source / "build_tools/runtime_transition_report.py").write_text(
            "raise SystemExit(99)\n"
        )
        output = self.root / "publication-output"
        with patch.dict(
            os.environ,
            {
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_SHA": commit,
                "GITHUB_OUTPUT": str(output),
            },
        ), patch.object(rt, "SCRIPT_DIR", source / "build_tools"):
            transition = rt.Transition(self.args, self.plan)
            transition.checkpoint()
            self.assertEqual(
                rt.prepare_publication(
                    transition.work,
                    "https://github.com/ROCm/TheRock/actions/runs/123/artifacts/456",
                ),
                0,
            )
        self.assertEqual(
            output.read_text(),
            f"sha256={rt.digest(transition.work / 'reports/report.json')}\n",
        )

    def test_archive_attributes_cannot_change_the_pinned_git_tree(self) -> None:
        source, commit = tool_source(self.root)
        self.args.therock_checkout = source
        for index, attribute in enumerate(("export-subst", "export-ignore")):
            with self.subTest(attribute=attribute):
                extra = source / "build_tools/extra.txt"
                extra.write_text("$Format:%H$\n")
                (source / ".gitattributes").write_text(
                    f"build_tools/extra.txt {attribute}\n"
                )
                git(source, "add", ".")
                git(source, "commit", "-m", "Archive attribute fixture")
                self.args.therock_ref = git(source, "rev-parse", "HEAD")
                self.args.work_dir = self.root / f"attributes-{index}"
                with self.assertRaisesRegex(rt.PrerequisiteError, "source tree"):
                    rt.create_orchestration_snapshot(self.args)
                git(source, "reset", "--hard", commit)

    def test_corrupt_mismatched_or_missing_snapshot_prevents_reporter_execution(
        self,
    ) -> None:
        source, commit = tool_source(self.root)
        self.args.therock_checkout = source
        self.args.therock_ref = commit
        for kind in (
            "content",
            "archive",
            "metadata",
            "symlink",
            "extra",
            "missing",
            "source",
        ):
            with self.subTest(kind=kind):
                self.args.work_dir = self.root / kind
                snapshot = rt.create_orchestration_snapshot(self.args)
                reporter = snapshot / "build_tools/runtime_transition_report.py"
                if kind == "content":
                    reporter.chmod(0o644)
                    reporter.write_text("raise SystemExit(99)\n")
                elif kind == "archive":
                    archive = snapshot / "build-tools.tar"
                    archive.chmod(0o644)
                    with archive.open("ab") as output:
                        output.write(b"corrupt")
                elif kind == "metadata":
                    metadata = snapshot / "snapshot.json"
                    metadata.chmod(0o644)
                    value = json.loads(metadata.read_text())
                    value["content_sha256"] = "0" * 64
                    metadata.write_text(json.dumps(value))
                elif kind in {"symlink", "missing"}:
                    reporter.parent.chmod(0o755)
                    reporter.unlink()
                    if kind == "symlink":
                        reporter.symlink_to(
                            source / "build_tools/runtime_transition_report.py"
                        )
                elif kind == "extra":
                    snapshot.chmod(0o755)
                    (snapshot / "extra.py").write_text("raise SystemExit(99)\n")
                else:
                    self.args.therock_ref = "f" * 40
                with patch.object(rt.subprocess, "call") as execute, self.assertRaises(
                    rt.PrerequisiteError
                ):
                    rt.finish(rt.Transition(self.args, self.plan))
                execute.assert_not_called()
                self.assertFalse(self.args.work_dir.exists())
                self.args.therock_ref = commit
        self.args.work_dir = self.root / "absent"
        with self.assertRaises(rt.PrerequisiteError):
            rt.finish(rt.Transition(self.args, self.plan))

    def test_missing_commit_and_helper_refuse_launch_before_docker(self) -> None:
        source, commit = tool_source(self.root)
        self.args.work_dir = self.root / "missing-commit"
        self.args.therock_ref = "f" * 40
        with patch.object(rt, "SCRIPT_DIR", source / "build_tools"), patch.object(
            rt.subprocess, "call"
        ) as execute:
            with self.assertRaisesRegex(
                rt.PrerequisiteError, "requested source commit"
            ):
                rt.launch_container(self.args, self.plan)
            execute.assert_not_called()
        for index, name in enumerate(sorted(rt.snapshot.ORCHESTRATION_HELPERS)):
            with self.subTest(helper=name):
                self.args.work_dir = self.root / f"missing-helper-{index}"
                git(source, "rm", name)
                git(source, "commit", "-m", "Remove helper")
                self.args.therock_ref = git(source, "rev-parse", "HEAD")
                self.args.therock_checkout = source
                with patch.object(
                    rt.subprocess, "call"
                ) as execute, self.assertRaisesRegex(
                    rt.PrerequisiteError, "required orchestration helpers"
                ):
                    rt.launch_container(self.args, self.plan)
                execute.assert_not_called()
                git(source, "reset", "--hard", commit)

    def test_snapshot_archive_rejects_unsafe_entries_and_bounds(self) -> None:
        archive_path = self.root / "unsafe.tar"
        for name, kind in (
            ("build_tools/../../escape", tarfile.REGTYPE),
            ("/build_tools/escape", tarfile.REGTYPE),
            ("build_tools/link", tarfile.SYMTYPE),
            ("build_tools/link", tarfile.LNKTYPE),
            ("build_tools/fifo", tarfile.FIFOTYPE),
            ("other/file", tarfile.REGTYPE),
        ):
            with self.subTest(name=name, kind=kind):
                with tarfile.open(
                    archive_path, "w", pax_headers={"comment": self.args.therock_ref}
                ) as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = (
                        "../../escape"
                        if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}
                        else ""
                    )
                    archive.addfile(member, io.BytesIO())
                with self.assertRaisesRegex(rt.snapshot.SnapshotError, "Unsafe"):
                    rt.snapshot.orchestration_files(
                        archive_path, self.args.therock_ref, self.root / "extract"
                    )
        self.assertFalse((self.root / "escape").exists())
        source, commit = tool_source(self.root)
        self.args.therock_checkout, self.args.therock_ref = source, commit
        snapshot = rt.create_orchestration_snapshot(self.args)
        for limit in ("MAX_ORCHESTRATION_BYTES", "MAX_ORCHESTRATION_ENTRIES"):
            with self.subTest(limit=limit), patch.object(
                rt.snapshot, limit, 1
            ), self.assertRaises(rt.PrerequisiteError):
                rt.verify_orchestration_snapshot(self.run.work, commit)

    def test_nested_build_environment_and_cargo_configuration_match_jobs(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        cmake = binary / "cmake"
        cmake.write_text(
            f"#!{sys.executable}\n"
            "import json,os,subprocess,sys\nfrom pathlib import Path\n"
            "if '--build' in sys.argv:\n"
            " subprocess.run([sys.executable, '-c', 'import json,os; print(json.dumps({key:os.environ.get(key) for key in (\"CMAKE_BUILD_PARALLEL_LEVEL\",\"CARGO_BUILD_JOBS\")}))'], check=True)\n"
            " sys.exit(1)\n"
            "build = Path(sys.argv[sys.argv.index('-B')+1])\n"
            "build.mkdir(parents=True)\n"
            "(build/'CMakeCache.txt').write_text('\\n'.join(argument[2:].split('=')[0]+':BOOL='+argument.split('=',1)[1] for argument in sys.argv if argument.startswith('-DTHEROCK_')))\n"
        )
        cmake.chmod(0o755)
        with patch.object(
            rt, "SYSTEM_PATH", f"{binary}:{rt.SYSTEM_PATH}"
        ), patch.object(self.run, "prepare_source", return_value=self.root):
            self.run.run_profile("baseline")
        configure = json.loads(
            (self.run.work / "diagnostics/command-0001.json").read_text()
        )
        self.assertIn(
            f"-DROCM_RUNTIMES_CARGO_JOBS={self.args.jobs}", configure["command"]
        )
        for key in (
            "THEROCK_AMDGPU_FAMILIES",
            "THEROCK_DIST_AMDGPU_FAMILIES",
            "THEROCK_TEST_AMDGPU_FAMILIES",
        ):
            self.assertIn(f"-D{key}={self.args.gpu_family}", configure["command"])
        nested = json.loads(
            (self.run.work / "diagnostics/command-0002.stdout").read_text()
        )
        self.assertEqual(
            nested,
            {
                "CMAKE_BUILD_PARALLEL_LEVEL": str(self.args.jobs),
                "CARGO_BUILD_JOBS": str(self.args.jobs),
            },
        )
        self.assertEqual(
            self.run.jobs["dist"].observations["baseline"]["status"], "failure"
        )
        self.assertNotIn("CMAKE_BUILD_PARALLEL_LEVEL", rt.clean_environment(self.root))
        self.assertNotIn("CARGO_BUILD_JOBS", rt.clean_environment(self.root))

    def test_native_disabled_gtest_projection_matches_default_inventory(self) -> None:
        raw = self.root / "gtest.xml"
        output = self.root / "selected.junit"
        raw.write_text(
            '<testsuites tests="2" failures="0" disabled="1"><testsuite tests="2" failures="0" skipped="0" disabled="1"><testcase classname="S" name="Run" status="run"/><testcase classname="S" name="DISABLED_Later" status="notrun" result="suppressed"/></testsuite></testsuites>'
        )
        rt.normalize_junit(raw, output, rt.Suite("ref", "hsa", "gtest"))
        self.assertEqual(
            [case.get("name") for case in ET.parse(output).iter("testcase")], ["Run"]
        )
        self.assertEqual(ET.parse(output).getroot().get("tests"), "1")
        self.assertIn("DISABLED_Later", raw.read_text())

    def test_junit_aggregate_contradiction_is_not_erased(self) -> None:
        raw = self.root / "raw.xml"
        output = self.root / "public.junit"
        raw.write_text(
            '<testsuites tests="2" failures="1"><testsuite tests="1" failures="0"><testcase name="A"/></testsuite></testsuites>'
        )
        rt.normalize_junit(raw, output, self.plan.suites[0])
        self.assertEqual(ET.parse(output).getroot().get("tests"), "2")
        self.assertEqual(ET.parse(output).getroot().get("failures"), "1")

    def test_runtime_selftests_select_private_image_in_baseline(self) -> None:
        build = self.root / "private-build"
        private = build / "core/rocm-runtimes/build/upstream/lib/libhsa-runtime64.so.1"
        private.parent.mkdir(parents=True)
        private.write_bytes(b"private-subject")
        stage = (
            build
            / "core/rocm-runtimes/stage/lib/rocm-runtimes/lib/libhsa-runtime64.so.1"
        )
        stage.parent.mkdir(parents=True)
        stage.write_bytes(private.read_bytes())
        suite = rt.Suite(
            "runtimes-host",
            "rocm-runtimes",
            "ctest",
            "core/rocm-runtimes/build",
            label="host",
        )
        self.run.jobs["inventory-runtimes-host"] = rt.Job(
            "inventory-runtimes-host", "rocm-runtimes", []
        )
        environments = []

        def measure(command: list[str], **kwargs: object) -> rt.CommandResult:
            environments.append(kwargs["env"])
            return self.fake_ctest(command, **kwargs)

        with patch.object(self.run.runner, "run", side_effect=measure):
            self.run.test_suite(
                "baseline",
                self.root,
                build,
                rt.clean_environment(build / "dist/rocm"),
                suite,
            )
        self.assertEqual(
            environments[0]["RUNTIME_TRANSITION_HSA"], str(private.resolve())
        )
        self.assertTrue(
            environments[0]["LD_LIBRARY_PATH"].startswith(str(private.parent))
        )
        self.assertEqual(
            self.run.jobs["test-runtimes-host"].test_identity["subject"],
            "runtime-selftests",
        )

    def test_existing_component_script_interposer_captures_effective_selection(
        self,
    ) -> None:
        fake = self.root / "ctest"
        fake.write_text(
            "#!"
            + sys.executable
            + "\n"
            + "import json,sys\nfrom pathlib import Path\n"
            + "if '--show-only=json-v1' in sys.argv:\n print(json.dumps({'kind':'ctestInfo','tests':[{'name':'SelectedFull'}]}))\n"
            + "elif '--output-junit' in sys.argv:\n Path(sys.argv[sys.argv.index('--output-junit')+1]).write_text('<testsuite tests=\"1\" failures=\"0\"><testcase name=\"SelectedFull\"/></testsuite>')\n"
        )
        fake.chmod(0o755)
        output = self.root / "component"
        output.mkdir()
        environment = {
            **os.environ,
            "RUNTIME_TRANSITION_CTEST": str(fake),
            "RUNTIME_TRANSITION_SUITE_OUTPUT": str(output),
        }
        subprocess.run(
            [
                sys.executable,
                str(rt.SUPPORT_DIR / "ctest_driver.py"),
                "--test-dir",
                "selected",
                "--label-regex",
                "^full$",
            ],
            check=True,
            env=environment,
        )
        inventory = next(output.glob("*.inventory.json"))
        self.assertEqual(
            rt.ctest_inventory(inventory, "component"),
            [{"classname": "component", "name": "SelectedFull"}],
        )
        self.assertTrue(list(output.glob("*.xml")))

    def test_public_transport_omits_raw_native_artifacts(self) -> None:
        text = (
            rt.SCRIPT_DIR.parent / ".github/workflows/runtime_transition.yml"
        ).read_text()
        self.assertNotIn("/manifest.json", text)
        self.assertNotIn("report-*.junit", text)
        self.assertIn("EXPECTED_REPORT_SHA256", text)
        self.assertIn("runtime_transition.py publish-report", text)

    def test_publication_requires_producer_digest_before_import_or_write(self) -> None:
        report = self.root / "report.json"
        report.write_text("{}")
        with patch.object(
            rt.importlib, "import_module"
        ) as module, self.assertRaisesRegex(ValueError, "digest"):
            rt.publish_report(report, "a" * 64, "OWNER/REPO")
        module.assert_not_called()

    def test_missing_build_tool_blocks_both_profiles_before_source_fetch(self) -> None:
        transition = rt.Transition(
            argparse.Namespace(
                **{**vars(self.args), "work_dir": self.root / "missing-tool"}
            ),
            self.plan,
        )
        with patch.object(
            rt, "validate_image_provenance", return_value={"fixture": True}
        ), patch.object(
            rt.shutil,
            "which",
            side_effect=lambda tool, **kwargs: (
                None if tool == "patch" else "/usr/bin/" + tool
            ),
        ), patch.object(
            transition, "run_profile"
        ) as profile:
            self.assertEqual(transition.run(), 1)
        profile.assert_not_called()
        for role in rt.PROVIDERS:
            observed = transition.jobs["prerequisites"].observations[role]
            self.assertEqual(observed["status"], "infrastructure_failure")
            error = json.loads(
                (
                    transition.work / "diagnostics" / f"{role}-prerequisites-error.json"
                ).read_text()
            )
            self.assertEqual(error["error"], "Missing build prerequisites: patch")

    def test_preflight_accepts_a_complete_build_image(self) -> None:
        with patch.object(
            rt.shutil, "which", side_effect=lambda tool, **kwargs: "/usr/bin/" + tool
        ):
            rt.verify_build_tools()

    def test_inspected_parent_and_provisioning_metadata_must_match(self) -> None:
        path = self.root / "provisioning.json"
        path.write_text(json.dumps({"parent_image": "parent@sha256:" + "a" * 64}))
        with self.assertRaisesRegex(rt.PrerequisiteError, "provisioning metadata"):
            rt.validate_image_provenance(
                {"Labels": {}, "RuntimeOptions": rt.GPU_CONTAINER_OPTIONS}, path
            )

    def test_workflow_configuration_rejects_non_digest_image_and_generic_pool(
        self,
    ) -> None:
        environment = {
            "TRANSITION_IMAGE": "image:latest",
            "TRANSITION_GPU": "gfx942",
            "TRANSITION_GPU_FAMILY": "gfx94X-dcgpu",
            "TRANSITION_RUNNER_LABELS": '["self-hosted","linux","x64","test-pool"]',
            "GITHUB_OUTPUT": str(self.root / "output"),
        }
        with patch.dict(os.environ, environment), self.assertRaisesRegex(
            ValueError, "pinned by digest"
        ):
            rt.workflow_configuration()
        environment["TRANSITION_IMAGE"] = "registry/image@sha256:" + "a" * 64
        environment["TRANSITION_RUNNER_LABELS"] = '["self-hosted","linux","x64"]'
        with patch.dict(os.environ, environment), self.assertRaisesRegex(
            ValueError, "dedicated pool"
        ):
            rt.workflow_configuration()

    def test_workflow_pins_permissions_finalization_and_trusted_dispatch(self) -> None:
        workflow = rt.SCRIPT_DIR.parent / ".github/workflows/runtime_transition.yml"
        text = workflow.read_text()
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("id-token:", text)
        self.assertIn("RUNTIME_TRANSITION_WORKER_SHA", text)
        self.assertIn('--runtime-ref "${TRANSITION_RUNTIME_REF}"', text)
        self.assertIn("runtime_transition.py finish", text)
        self.assertIn("if: always()", text)
        self.assertNotIn("issues: write", text.split("  publish:")[0])
        for line in text.splitlines():
            if "uses: actions/" in line:
                self.assertRegex(line, r"@[0-9a-f]{40} ")
        dispatcher = (workflow.parent / "runtime_transition_schedule.yml").read_text()
        self.assertNotIn("schedule:", dispatcher)
        self.assertIn("RUNTIME_TRANSITION_RUNTIME_REF", dispatcher)


if __name__ == "__main__":
    unittest.main()
