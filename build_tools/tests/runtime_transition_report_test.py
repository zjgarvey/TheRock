#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Evidence-driven reporter tests. All publication I/O stays in memory."""

import copy
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_transition_report as reporter

SOURCE = "1" * 40
REPOSITORY = "example/qualification"
RUN_URL = f"https://github.com/{REPOSITORY}/actions/runs/123"
TEST = {"classname": "Smoke", "name": "Allocate"}
OTHER_TEST = {"classname": "Smoke", "name": "Release"}
ENVIRONMENT = {
    "os": "ubuntu-24.04",
    "kernel": "6.8.0",
    "driver": "6.8.0-amdgpu",
    "gpu": "gfx942",
    "container": "sha256:" + "a" * 64,
}


def junit(*cases: tuple[dict[str, str], str]) -> str:
    """Produce realistic JUnit, including diagnostics that must remain private."""
    elements = []
    for identity, outcome in cases:
        terminal = (
            ""
            if outcome == "success"
            else (
                f'<{outcome} message="/home/private-user/log token=do-not-publish">'
                f"private diagnostics</{outcome}>"
            )
        )
        elements.append(
            f'<testcase classname={quoteattr(identity["classname"])} name={quoteattr(identity["name"])} time="0.1">'
            f"{terminal}<system-out>secret log payload</system-out></testcase>"
        )
    return (
        '<testsuites><testsuite name="smoke">'
        + "".join(elements)
        + "</testsuite></testsuites>"
    )


class FakeGitHub:
    """Stateful API boundary: pagination, writes and reads follow actual URLs."""

    def __init__(self):
        self.issues = []
        self.requests = []
        self.repository = {
            "full_name": REPOSITORY,
            "default_branch": "main",
            "fork": False,
        }
        self.run = {
            "id": 123,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "head_sha": SOURCE,
            "head_branch": "main",
            "html_url": RUN_URL,
            "head_repository": {"full_name": REPOSITORY},
            "pull_requests": [],
        }

    def send_request(
        self, url: str, *, method: str = "GET", body: object = None
    ) -> object:
        self.requests.append((method, url, copy.deepcopy(body)))
        parsed = urlsplit(url)
        source_base = f"/repos/{REPOSITORY}"
        issue_base = "/repos/example/burn-down/issues"
        if parsed.path == source_base:
            return copy.deepcopy(self.repository)
        if parsed.path == source_base + "/actions/runs/123":
            return copy.deepcopy(self.run)
        if parsed.path == issue_base and method == "GET":
            query = parse_qs(parsed.query)
            assert query["state"] == ["all"]
            page = int(query["page"][0])
            return copy.deepcopy(self.issues[(page - 1) * 100 : page * 100])
        if parsed.path == issue_base and method == "POST":
            issue = {"number": len(self.issues) + 1, "state": "open", **body}
            self.issues.append(issue)
            return copy.deepcopy(issue)
        if parsed.path.startswith(issue_base + "/"):
            number = int(parsed.path.rsplit("/", 1)[1])
            issue = next(issue for issue in self.issues if issue["number"] == number)
            if method == "PATCH":
                issue.update(body)
            return copy.deepcopy(issue)
        raise AssertionError(f"Unexpected request {method} {url}")


class ReporterFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "manifest.json"
        self.manifest = {
            "schema_version": 1,
            "repository": REPOSITORY,
            "run": {
                "id": "123",
                "attempt": 1,
                "event": "workflow_dispatch",
                "ref": "refs/heads/main",
                "sha": SOURCE,
                "url": RUN_URL,
            },
            "profiles": {},
            "jobs": [
                {
                    "id": "hip-smoke-gfx942",
                    "kind": "test",
                    "component": "hip",
                    "needs": [],
                    "test_identity": {
                        "suite": "hip-smoke",
                        "revision": SOURCE,
                        "configuration": "release",
                    },
                    "expected_tests": [TEST],
                    "observations": {},
                }
            ],
        }
        for role, provider, flag in (
            ("baseline", "rocr", "OFF"),
            ("candidate", "rust-hsa", "ON"),
        ):
            self.manifest["profiles"][role] = {
                "provider": provider,
                "sources": {
                    "therock": SOURCE,
                    "runtime": ("2" if role == "baseline" else "3") * 40,
                },
                "toolchain": {"id": "therock-compiler-7.14", "sha256": "a" * 64},
                "configuration": {
                    "build_type": "Release",
                    "runtime_flag": flag,
                    "hip_flag": "ON",
                },
            }
            self.observations[role] = {
                "status": "success",
                "started": True,
                "environment": dict(ENVIRONMENT),
                "evidence_url": RUN_URL
                + ("/job/456" if role == "baseline" else "/job/457"),
                "reproduction_url": f"https://github.com/{REPOSITORY}/blob/{SOURCE}/tests/README.md",
                "junit": [],
                "executed_tests": [],
            }
            self.result(role, junit((TEST, "success")))

    @property
    def observations(self) -> dict:
        return self.manifest["jobs"][0]["observations"]

    def result(self, role: str, xml: str, *, status: str = "success") -> None:
        content = xml.encode()
        file_path = self.root / f"{role}.xml"
        file_path.write_bytes(content)
        self.observations[role]["junit"] = [
            {"path": file_path.name, "sha256": hashlib.sha256(content).hexdigest()}
        ]
        self.observations[role]["status"] = status

    def report(self) -> dict:
        self.path.write_text(json.dumps(self.manifest), encoding="utf-8")
        return reporter.build_report(reporter.load_manifest(self.path))

    def reasons(self, report: dict, role: str = "candidate") -> list:
        return report["jobs"][0]["observations"][role]["coverage_reasons"]

    def candidate_failure(self) -> dict:
        self.result("candidate", junit((TEST, "failure")), status="failure")
        return self.report()

    def local(self) -> None:
        self.manifest["run"].update(
            id="local-gfx942-qualification", event="local", url=None
        )
        for job in self.manifest["jobs"]:
            for role, observation in job["observations"].items():
                observation.update(
                    evidence_url=None,
                    reproduction_url=None,
                    evidence_locator=f"evidence/{role}/result.json",
                    reproduction_locator="build_tools/runtime_transition.py",
                )


class ReporterTest(ReporterFixture):
    def test_complete_paired_run_qualifies_with_exact_identities(self):
        report = self.report()
        self.assertTrue(report["qualified"])
        self.assertTrue(report["comparable"])
        self.assertEqual(report["profiles"], self.manifest["profiles"])
        observation = report["jobs"][0]["observations"]["candidate"]
        self.assertEqual(
            [
                {k: v for k, v in t.items() if k != "identity_sha256"}
                for t in observation["tests"]
            ],
            [{**TEST, "outcome": "success"}],
        )
        self.assertEqual(observation["environment"], ENVIRONMENT)
        self.assertEqual(
            observation["junit"][0]["sha256"],
            self.observations["candidate"]["junit"][0]["sha256"],
        )

    def test_failure_classifications_are_paired_per_test(self):
        for baseline, candidate, expected in (
            ("success", "failure", "candidate_only"),
            ("failure", "success", "baseline_only"),
            ("failure", "error", "shared"),
        ):
            with self.subTest(expected=expected):
                self.result(
                    "baseline",
                    junit((TEST, baseline)),
                    status="success" if baseline == "success" else "failure",
                )
                self.result(
                    "candidate",
                    junit((TEST, candidate)),
                    status="success" if candidate == "success" else "failure",
                )
                report = self.report()
                self.assertFalse(report["qualified"])
                self.assertTrue(report["comparable"])
                self.assertEqual(
                    [f["classification"] for f in report["findings"]], [expected]
                )
                self.assertEqual(len(report["issue_candidates"]), 1)

    def test_missing_cancelled_skipped_and_infrastructure_are_never_success(self):
        for status in ("cancelled", "skipped", "infrastructure_failure"):
            with self.subTest(status=status):
                self.observations["candidate"]["status"] = status
                report = self.report()
                self.assertFalse(report["qualified"])
                self.assertFalse(report["comparable"])
                self.assertIn(status, self.reasons(report))
                self.assertEqual(report["issue_candidates"], [])
        del self.observations["candidate"]
        report = self.report()
        self.assertIn("missing_observation", self.reasons(report))
        self.assertNotIn("identity_mismatch", self.reasons(report))
        self.assertFalse(report["qualified"])

    def test_no_started_command_cannot_claim_a_test_failure(self):
        self.candidate_failure()
        self.observations["candidate"]["started"] = False
        report = self.report()
        self.assertIn("command_not_started", self.reasons(report))
        self.assertEqual(report["issue_candidates"], [])

    def test_zero_tests_and_absent_artifacts_are_incomplete(self):
        for xml in ("<testsuite tests='0'/>", "<testsuites/>"):
            self.result("candidate", xml)
            report = self.report()
            self.assertIn("zero_tests", self.reasons(report))
            self.assertFalse(report["qualified"])
        self.observations["candidate"]["junit"] = []
        self.assertIn("missing_tests", self.reasons(self.report()))

    def test_skip_is_not_coverage_even_with_successful_job(self):
        self.result("candidate", junit((TEST, "skipped")))
        report = self.report()
        self.assertIn("skipped_tests", self.reasons(report))
        self.assertFalse(report["qualified"])
        self.assertEqual(report["issue_candidates"], [])

    def test_bad_missing_changed_and_duplicate_xml_fail_closed(self):
        for xml in (
            "<testsuite",
            "<testsuite tests='2'><testcase classname='Smoke' name='Allocate'/></testsuite>",
            junit((TEST, "success"), (TEST, "success")),
        ):
            with self.subTest(xml=xml):
                self.result("candidate", xml)
                report = self.report()
                self.assertIn("invalid_junit", self.reasons(report))
                self.assertEqual(report["issue_candidates"], [])
        self.result("candidate", junit((TEST, "success")))
        (self.root / "candidate.xml").write_text("<testsuite/>")
        self.assertIn("invalid_junit", self.reasons(self.report()))
        (self.root / "candidate.xml").unlink()
        self.assertIn("unreadable_junit", self.reasons(self.report()))

    def test_multiple_files_merge_and_duplicate_results_are_ambiguous(self):
        self.manifest["jobs"][0]["expected_tests"].append(OTHER_TEST)
        for role in reporter.ROLES:
            extra = self.root / f"{role}-extra.xml"
            content = junit((OTHER_TEST, "success")).encode()
            extra.write_bytes(content)
            self.observations[role]["junit"].append(
                {"path": extra.name, "sha256": hashlib.sha256(content).hexdigest()}
            )
        self.assertTrue(self.report()["qualified"])
        self.result("candidate", junit((TEST, "success"), (OTHER_TEST, "success")))
        extra = self.root / "candidate-extra.xml"
        self.observations["candidate"]["junit"].append(
            {
                "path": extra.name,
                "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
            }
        )
        self.assertIn("invalid_junit", self.reasons(self.report()))

    def test_individual_timeout_requires_observed_test_start(self):
        self.observations["candidate"].update(
            status="timed_out", junit=[], executed_tests=[TEST]
        )
        report = self.report()
        candidate = report["issue_candidates"][0]
        self.assertEqual(candidate["finding"]["classification"], "candidate_only")
        self.assertEqual(candidate["finding"]["outcomes"]["candidate"], "timed_out")
        self.assertTrue(report["comparable"])
        self.observations["candidate"]["executed_tests"] = []
        report = self.report()
        self.assertIn("unattributed_timeout", self.reasons(report))
        self.assertEqual(report["issue_candidates"], [])

    def test_timeout_with_multiple_unreported_starts_is_not_attributed(self):
        self.manifest["jobs"][0]["expected_tests"].append(OTHER_TEST)
        self.observations["candidate"].update(
            status="timed_out", junit=[], executed_tests=[TEST, OTHER_TEST]
        )
        report = self.report()
        self.assertEqual(report["issue_candidates"], [])
        self.assertIn("unattributed_timeout", self.reasons(report))

    def test_partial_timeout_preserves_measured_failure_and_missing_coverage(self):
        self.manifest["jobs"][0]["expected_tests"].append(OTHER_TEST)
        self.result("baseline", junit((TEST, "success"), (OTHER_TEST, "success")))
        self.result("candidate", junit((TEST, "failure")), status="timed_out")
        report = self.report()
        self.assertFalse(report["comparable"])
        self.assertEqual(len(report["issue_candidates"]), 1)
        self.assertEqual(
            report["issue_candidates"][0]["finding"]["test"]["name"], TEST["name"]
        )
        self.assertIn("missing_tests", self.reasons(report))

    def test_runner_and_xml_disagreement_invalidates_results(self):
        for xml, status in (
            (junit((TEST, "failure")), "success"),
            (junit((TEST, "success")), "failure"),
        ):
            self.result("candidate", xml, status=status)
            report = self.report()
            self.assertIn("command_junit_disagreement", self.reasons(report))
            self.assertEqual(report["issue_candidates"], [])
            self.assertFalse(report["qualified"])

    def test_environment_and_source_mismatch_prevents_provider_attribution(self):
        self.candidate_failure()
        changes = [
            (self.observations["candidate"]["environment"], "driver", "other-driver"),
            (self.manifest["profiles"]["candidate"]["toolchain"], "sha256", "b" * 64),
            (self.manifest["profiles"]["candidate"]["sources"], "therock", "f" * 40),
            (
                self.manifest["profiles"]["candidate"]["configuration"],
                "hip_flag",
                "OFF",
            ),
        ]
        for mapping, key, value in changes:
            old = mapping[key]
            mapping[key] = value
            with self.subTest(key=key):
                report = self.report()
                self.assertEqual(report["issue_candidates"], [])
                self.assertIn("identity_mismatch", self.reasons(report))
                self.assertFalse(report["comparable"])
            mapping[key] = old

    def test_missing_or_unexpected_inventory_is_not_success(self):
        self.manifest["jobs"][0]["expected_tests"].append(OTHER_TEST)
        self.assertIn("missing_tests", self.reasons(self.report()))
        self.manifest["jobs"][0]["expected_tests"] = [TEST]
        self.result("candidate", junit((TEST, "success"), (OTHER_TEST, "success")))
        report = self.report()
        self.assertIn("unexpected_tests", self.reasons(report))
        self.assertFalse(report["qualified"])

    def test_build_failure_blocks_descendants_without_issue_spam(self):
        build = {
            "id": "build",
            "kind": "build",
            "component": "runtime",
            "needs": [],
            "observations": copy.deepcopy(self.observations),
        }
        for role, observation in build["observations"].items():
            del observation["junit"]
            del observation["executed_tests"]
            observation["status"] = "failure" if role == "candidate" else "success"
        self.manifest["jobs"][0]["needs"] = ["build"]
        self.manifest["jobs"].append(build)
        self.candidate_failure()
        report = self.report()
        self.assertEqual(len(report["issue_candidates"]), 1)
        self.assertEqual(report["issue_candidates"][0]["finding"]["job"], "build")
        child = next(j for j in report["jobs"] if j["kind"] == "test")
        self.assertEqual(child["observations"]["candidate"]["blocked_by"], ["build"])
        self.assertFalse(report["qualified"])

    def test_build_only_coverage_never_qualifies_a_provider_switch(self):
        job = self.manifest["jobs"][0]
        job["kind"] = "build"
        del job["test_identity"]
        del job["expected_tests"]
        for observation in self.observations.values():
            del observation["junit"]
            del observation["executed_tests"]
        report = self.report()
        self.assertFalse(report["qualified"])
        self.assertFalse(report["comparable"])
        self.assertIn("no_test_jobs", report["findings"][0]["reasons"])

    def test_build_timeout_requires_started_command_and_generates_one_root_issue(self):
        job = copy.deepcopy(self.manifest["jobs"][0])
        job.update(id="runtime-build", kind="build")
        del job["test_identity"]
        del job["expected_tests"]
        for observation in job["observations"].values():
            del observation["junit"]
            del observation["executed_tests"]
        job["observations"]["candidate"]["status"] = "timed_out"
        self.manifest["jobs"].append(job)
        report = self.report()
        self.assertEqual(
            report["issue_candidates"][0]["finding"]["classification"], "candidate_only"
        )
        job["observations"]["candidate"]["started"] = False
        self.assertEqual(self.report()["issue_candidates"], [])

    def test_candidate_bound_is_explicit_and_keeps_full_failure_findings(self):
        self.manifest["jobs"][0]["expected_tests"].append(OTHER_TEST)
        self.result("baseline", junit((TEST, "success"), (OTHER_TEST, "success")))
        self.result(
            "candidate",
            junit((TEST, "failure"), (OTHER_TEST, "failure")),
            status="failure",
        )
        with mock.patch.object(reporter, "MAX_ISSUE_CANDIDATES", 1):
            report = self.report()
        self.assertEqual(report["issue_candidates_overflow"], 1)
        self.assertEqual(len(report["issue_candidates"]), 1)
        self.assertEqual(len(report["findings"]), 2)
        self.assertFalse(report["qualified"])

    def test_real_cpp_parameter_names_remain_exact(self):
        name = "Copy<std::pair<int, bool?>*>/0 [a&b] `{parameter}`"
        identity = {"classname": "Memory<int>", "name": name}
        self.manifest["jobs"][0]["expected_tests"] = [identity]
        xml = '<testsuite tests="1"><testcase classname="Memory&lt;int&gt;" name="Copy&lt;std::pair&lt;int, bool?&gt;*&gt;/0 [a&amp;b] `{parameter}`"/></testsuite>'
        for role in reporter.ROLES:
            self.result(role, xml)
        report = self.report()
        self.assertTrue(report["qualified"])
        self.assertEqual(
            report["jobs"][0]["observations"]["candidate"]["tests"][0]["name"], name
        )

    def test_private_test_parameters_are_hashed_without_losing_identity(self):
        keys = []
        for name in (
            "param[/home/private/secret]",
            "token=private",
            "bad\nname",
            "C:\\private\\source.cpp",
        ):
            identity = {"classname": "Memory", "name": name}
            self.manifest["jobs"][0]["expected_tests"] = [identity]
            self.result("baseline", junit((identity, "success")))
            self.result("candidate", junit((identity, "failure")), status="failure")
            report = self.report()
            self.assertTrue(report["comparable"])
            candidate = report["issue_candidates"][0]
            self.assertEqual(candidate["finding"]["classification"], "candidate_only")
            self.assertTrue(
                candidate["finding"]["test"]["name"].startswith("redacted-sha256:")
            )
            self.assertNotIn(name, json.dumps(report))
            self.assertEqual(
                candidate["dedupe_key"],
                self.report()["issue_candidates"][0]["dedupe_key"],
            )
            keys.append(candidate["dedupe_key"])
        self.assertEqual(len(set(keys)), len(keys))

    def test_real_configuration_and_compiler_headers_are_public_values(self):
        for profile in self.manifest["profiles"].values():
            profile["configuration"]["targets"] = "gfx942;gfx950"
            profile["configuration"]["compile_options"] = "-O2 -g -DNDEBUG"
            profile["toolchain"][
                "id"
            ] = "clang version 21.0.0 (https://github.com/llvm/llvm-project.git)"
        self.assertTrue(self.report()["qualified"])

    def test_redacted_and_literal_hash_spellings_do_not_alias(self):
        private = {"classname": "Memory", "name": "/home/private/test"}
        literal = {
            "classname": "Memory",
            "name": "redacted-sha256:"
            + hashlib.sha256(private["name"].encode()).hexdigest(),
        }
        self.manifest["jobs"][0]["expected_tests"] = [private, literal]
        self.result("baseline", junit((private, "success"), (literal, "success")))
        self.result(
            "candidate",
            junit((private, "failure"), (literal, "success")),
            status="failure",
        )
        report = self.report()
        self.assertTrue(report["comparable"])
        self.assertEqual(len(report["issue_candidates"]), 1)
        tests = report["jobs"][0]["observations"]["candidate"]["tests"]
        self.assertEqual(tests[0]["name"], tests[1]["name"])
        self.assertNotEqual(tests[0]["identity_sha256"], tests[1]["identity_sha256"])

    def test_local_run_qualifies_offline_without_fake_urls(self):
        self.local()
        report = self.report()
        self.assertTrue(report["qualified"])
        self.assertIsNone(report["run"]["url"])
        reporter.write_report(report, self.root / "local")
        summary = (self.root / "local" / "summary.md").read_text()
        self.assertIn("Evidence is local", summary)
        self.assertIn("evidence/candidate/result.json", summary)
        self.assertNotIn("https://github.com", summary)
        args = [
            "--input",
            str(self.path),
            "--output-dir",
            str(self.root / "offline-cli"),
        ]
        with mock.patch.object(reporter, "GitHubAPI") as api:
            self.assertEqual(reporter.main(args), 0)
            api.assert_not_called()

    def test_local_failure_creates_unpublishable_draft_with_measured_evidence(self):
        self.local()
        report = self.candidate_failure()
        candidate = report["issue_candidates"][0]
        self.assertFalse(candidate["publishable"])
        self.assertEqual(candidate["finding"]["classification"], "candidate_only")
        self.assertIn("local evidence", candidate["body"])
        self.assertNotIn("https://github.com", candidate["body"])
        self.assertNotIn("](None)", candidate["body"])
        self.assertTrue(
            report["jobs"][0]["observations"]["candidate"]["junit"][0]["valid"]
        )

    def test_local_contract_refuses_fake_links_and_unsafe_locators(self):
        self.local()
        original = copy.deepcopy(self.manifest)
        mutations = [
            lambda: self.manifest["run"].update(url=RUN_URL),
            lambda: self.observations["candidate"].update(
                evidence_url=RUN_URL + "/job/1"
            ),
            lambda: self.observations["candidate"].update(
                evidence_locator="/home/private/build.log"
            ),
            lambda: self.observations["candidate"].update(
                evidence_locator="../outside.log"
            ),
            lambda: self.observations["candidate"].update(
                evidence_locator="logs/../outside.log"
            ),
            lambda: self.observations["candidate"].update(
                evidence_locator="logs//result.json"
            ),
            lambda: self.observations["candidate"].update(
                reproduction_url="file:///home/private/repro.sh"
            ),
        ]
        for mutation in mutations:
            self.manifest = copy.deepcopy(original)
            mutation()
            with self.assertRaises(reporter.ContractError):
                self.report()

    def test_dedupe_stable_across_revisions_runs_and_reclassification(self):
        self.manifest["jobs"][0]["test_identity"].update(
            subject="runtime-selftests", implementation_revision="6" * 40
        )
        first = self.candidate_failure()["issue_candidates"][0]
        self.result("baseline", junit((TEST, "failure")), status="failure")
        self.manifest["profiles"]["candidate"]["sources"]["runtime"] = "4" * 40
        self.manifest["jobs"][0]["test_identity"]["revision"] = "5" * 40
        self.manifest["jobs"][0]["test_identity"]["implementation_revision"] = "7" * 40
        self.manifest["run"]["attempt"] = 2
        second = self.report()["issue_candidates"][0]
        self.assertEqual(first["dedupe_key"], second["dedupe_key"])
        self.assertNotEqual(first["body"], second["body"])
        self.manifest["jobs"][0]["test_identity"]["subject"] = "canonical-provider"
        self.assertNotEqual(
            first["dedupe_key"], self.report()["issue_candidates"][0]["dedupe_key"]
        )
        self.manifest["jobs"][0]["test_identity"]["subject"] = "runtime-selftests"
        self.manifest["jobs"][0]["id"] = "hip-smoke-gfx950"
        self.assertNotEqual(
            first["dedupe_key"], self.report()["issue_candidates"][0]["dedupe_key"]
        )

    def test_dedupe_distinguishes_target_cells_without_changing_with_driver_updates(
        self,
    ):
        original = self.candidate_failure()["issue_candidates"][0]["dedupe_key"]
        for observation in self.manifest["jobs"][0]["observations"].values():
            observation["environment"]["driver"] = "updated-driver"
        self.assertEqual(original, self.report()["issue_candidates"][0]["dedupe_key"])
        for field, value in (
            ("gpu", "gfx1201"),
            ("gpu_count", "4"),
            ("os", "ubuntu-26.04"),
        ):
            with self.subTest(field=field):
                for observation in self.manifest["jobs"][0]["observations"].values():
                    observation["environment"][field] = value
                changed = self.report()["issue_candidates"][0]["dedupe_key"]
                self.assertNotEqual(original, changed)
                original = changed
        for profile in self.manifest["profiles"].values():
            profile["configuration"]["build_type"] = "Debug"
        self.assertNotEqual(
            original, self.report()["issue_candidates"][0]["dedupe_key"]
        )

    def test_dedupe_is_stable_when_local_evidence_becomes_public(self):
        public = self.candidate_failure()["issue_candidates"][0]
        self.local()
        local = self.report()["issue_candidates"][0]
        self.assertEqual(local["dedupe_key"], public["dedupe_key"])
        self.assertFalse(local["publishable"])
        self.assertTrue(public["publishable"])

    def test_report_is_deterministic_and_never_contains_raw_logs_or_paths(self):
        another = copy.deepcopy(self.manifest["jobs"][0])
        another["id"] = "second-job"
        self.manifest["jobs"].append(another)
        report = self.candidate_failure()
        first = self.root / "one"
        second = self.root / "two"
        reporter.write_report(report, first)
        self.manifest["jobs"].reverse()
        self.manifest["profiles"] = dict(
            reversed(list(self.manifest["profiles"].items()))
        )
        reporter.write_report(self.report(), second)
        for name in ("summary.md", "report.json", "issue-candidates.json"):
            content = (first / name).read_bytes()
            self.assertEqual(content, (second / name).read_bytes())
            for forbidden in (
                str(self.root),
                "/home/private-user",
                "do-not-publish",
                "secret log payload",
                "candidate.xml",
            ):
                self.assertNotIn(forbidden, content.decode())
        body = report["issue_candidates"][0]["body"]
        self.assertIn(RUN_URL + "/job/457", body)
        self.assertIn(self.observations["candidate"]["reproduction_url"], body)
        self.assertIn(SOURCE, body)

    def test_contract_rejects_unknown_duplicate_cyclic_and_unsafe_inputs(self):
        original = copy.deepcopy(self.manifest)
        mutations = [
            lambda: self.manifest.update(schema_version=True),
            lambda: self.manifest.update(extra="ignored"),
            lambda: self.manifest.update(jobs=[]),
            lambda: self.manifest["jobs"][0].update(expected_tests=[]),
            lambda: self.manifest["jobs"][0].update(needs=["hip-smoke-gfx942"]),
            lambda: self.manifest["jobs"][0].update(needs=["unknown"]),
            lambda: self.manifest["jobs"][0]["expected_tests"].append(TEST),
            lambda: self.observations["candidate"].update(status="unknown"),
            lambda: self.observations["candidate"].update(
                evidence_url="https://github.com/other/repo/actions/runs/123/job/456"
            ),
            lambda: self.observations["candidate"].update(
                reproduction_url=f"https://github.com/{REPOSITORY}/blob/main/tests/a.py"
            ),
            lambda: self.observations["candidate"]["environment"].update(
                driver="/home/private-driver"
            ),
            lambda: self.manifest["profiles"]["candidate"]["configuration"].update(
                extra="token=private"
            ),
            lambda: self.observations["candidate"]["environment"].update(
                GITHUB_TOKEN="innocent-looking-secret"
            ),
        ]
        for index, mutation in enumerate(mutations):
            self.manifest = copy.deepcopy(original)
            mutation()
            with self.subTest(index=index), self.assertRaises(reporter.ContractError):
                self.report()
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(reporter.ContractError):
            reporter.load_manifest(self.path)

    def test_bounds_fail_closed(self):
        self.path.write_bytes(b" " * (reporter.MAX_MANIFEST_BYTES + 1))
        with self.assertRaises(reporter.ContractError):
            reporter.load_manifest(self.path)
        with mock.patch.object(reporter, "MAX_XML_BYTES", 20):
            report = self.report()
        self.assertFalse(report["qualified"])
        self.assertEqual(report["issue_candidates"], [])
        with mock.patch.object(reporter, "MAX_JOBS", 0), self.assertRaises(
            reporter.ContractError
        ):
            self.report()

    def test_large_component_phase_inventory_is_iterative_and_bounded(self):
        phases = [
            {
                "id": f"phase-{i:04d}",
                "kind": "build",
                "component": "runtime",
                "needs": [f"phase-{i - 1:04d}"] if i else [],
                "observations": {},
            }
            for i in range(1023)
        ]
        self.manifest["jobs"][0]["needs"] = ["phase-1022"]
        self.manifest["jobs"] = list(reversed(phases + self.manifest["jobs"]))
        report = self.report()
        self.assertEqual(len(report["jobs"]), 1024)
        self.assertEqual(report["jobs"][0]["id"], "phase-0000")
        self.assertEqual(
            report["jobs"][-1]["observations"]["candidate"]["blocked_by"],
            ["phase-1022"],
        )
        self.assertFalse(report["qualified"])
        self.assertEqual(report["issue_candidates"], [])
        self.manifest["jobs"].append(
            {
                "id": "overflow",
                "kind": "build",
                "component": "runtime",
                "needs": [],
                "observations": {},
            }
        )
        with self.assertRaises(reporter.ContractError):
            self.report()

    def test_evidence_byte_depth_and_node_budgets_fail_closed(self):
        with mock.patch.object(reporter, "MAX_TOTAL_XML_BYTES", 1), self.assertRaises(
            reporter.EvidenceBudgetError
        ):
            self.report()
        with mock.patch.object(reporter, "MAX_XML_ELEMENTS", 1):
            self.assertIn("invalid_junit", self.reasons(self.report()))
        with mock.patch.object(reporter, "MAX_XML_DEPTH", 1):
            self.assertIn("invalid_junit", self.reasons(self.report()))

    def test_cli_invalid_json_fails_without_disclosing_input(self):
        for content in (
            "/home/private-secret",
            "[" * 1500 + "]" * 1500,
            '{"schema_version":' + "9" * 5000 + "}",
        ):
            self.path.write_text(content)
            errors = io.StringIO()
            with redirect_stderr(errors):
                code = reporter.main(
                    [
                        "--input",
                        str(self.path),
                        "--output-dir",
                        str(self.root / "refused"),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertNotIn("private-secret", errors.getvalue())
            self.assertNotIn(str(self.root), errors.getvalue())

    def test_cli_runs_from_other_directory_and_reports_exit_status(self):
        self.report()
        script = Path(reporter.__file__).resolve()
        command = [
            sys.executable,
            str(script),
            "--input",
            str(self.path),
            "--output-dir",
            str(self.root / "cli"),
        ]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.candidate_failure()
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((self.root / "cli" / "summary.md").exists())


class JUnitParserTest(unittest.TestCase):
    def test_realistic_pytest_gtest_and_ctest_shapes(self):
        samples = [
            b'<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0"><testcase classname="test_heap.TestHeap" name="test_allocate[32]" time="0.01"/></testsuite></testsuites>',
            b'<testsuites tests="1" failures="1" disabled="0"><testsuite name="Memory" tests="1" failures="1" disabled="0"><testcase name="Copy/0" classname="Memory"><failure message="assertion">stack</failure></testcase></testsuite></testsuites>',
            b'<testsuite name="(empty)" tests="1" failures="0" disabled="0"><testcase name="hip_smoke" classname="hip_smoke" status="run" time="0.001"><system-out>Passed</system-out></testcase></testsuite>',
            b'<testsuites xmlns="urn:junit"><testsuite tests="1"><testcase name="A"/></testsuite></testsuites>',
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(len(reporter.parse_junit(sample)), 1)

    def test_disabled_gtest_is_not_a_pass(self):
        results = reporter.parse_junit(
            b'<testsuite tests="1"><testcase name="DISABLED_A" status="notrun" result="suppressed"/></testsuite>'
        )
        self.assertEqual(list(results.values()), ["skipped"])

    def test_entities_suite_errors_hidden_counts_and_retries_are_rejected(self):
        for sample in (
            b'<!DOCTYPE a [<!ENTITY x "expanded">]><testsuite><testcase name="&x;"/></testsuite>',
            b'<testsuite tests="2"><testcase name="only-one"/></testsuite>',
            b'<testsuite failures="1"><testcase name="false-pass"/></testsuite>',
            b'<testsuite><testcase name="retry"><flakyFailure/></testcase></testsuite>',
            b'<testsuite><testcase name="both"><skipped/><failure/></testcase></testsuite>',
            b'<testsuite><testcase name="not-run" status="notrun"><failure/></testcase></testsuite>',
            b'<testsuite><error message="suite failed"/><testcase name="A"/></testsuite>',
            b'<testsuite><testcase name="A" result="unknown"/></testsuite>',
            b'<testsuite><properties><testcase name="A"/></properties></testsuite>',
            b'<testsuite><properties><testsuite><testcase name="A"/></testsuite></properties></testsuite>',
        ):
            with self.subTest(sample=sample), self.assertRaises(reporter.ContractError):
                reporter.parse_junit(sample)


class PublishingTest(ReporterFixture):
    def publish(
        self,
        report: dict,
        api: FakeGitHub,
        *,
        environment: dict | None = None,
        target: str = "example/burn-down",
    ) -> list:
        manifest = reporter.load_manifest(self.path)
        return reporter.publish_issues(
            manifest,
            report,
            issue_repository=target,
            environment=self.context() if environment is None else environment,
            api=api,
        )

    def context(self) -> dict:
        run = self.manifest["run"]
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_RUN_ID": run["id"],
            "GITHUB_RUN_ATTEMPT": str(run["attempt"]),
            "GITHUB_SHA": run["sha"],
            "GITHUB_EVENT_NAME": run["event"],
            "GITHUB_REF": run["ref"],
            "GITHUB_TOKEN": "mock-token",
        }

    def test_upsert_creates_once_updates_once_and_then_does_nothing(self):
        report = self.candidate_failure()
        api = FakeGitHub()
        self.assertEqual(self.publish(report, api)[0]["action"], "created")
        self.assertEqual(self.publish(report, api)[0]["action"], "unchanged")
        self.result("baseline", junit((TEST, "failure")), status="failure")
        changed = self.report()
        self.assertEqual(self.publish(changed, api)[0]["action"], "updated")
        self.assertEqual(self.publish(changed, api)[0]["action"], "unchanged")
        writes = [(method, url) for method, url, _ in api.requests if method != "GET"]
        self.assertEqual([method for method, _ in writes], ["POST", "PATCH"])
        self.assertTrue(
            all("/repos/example/burn-down/issues" in url for _, url in writes)
        )
        self.assertEqual(len(api.issues), 1)

    def test_closed_and_historical_issues_survive_incomplete_and_passing_runs(self):
        report = self.candidate_failure()
        api = FakeGitHub()
        self.publish(report, api)
        api.issues[0]["state"] = "closed"
        self.assertEqual(self.publish(report, api)[0]["action"], "preserved_closed")
        api.issues[0]["state"] = "open"
        previous = copy.deepcopy(api.issues)
        self.observations["candidate"]["status"] = "cancelled"
        self.assertEqual(self.publish(self.report(), api), [])
        self.result("candidate", junit((TEST, "success")))
        self.assertEqual(self.publish(self.report(), api), [])
        self.assertEqual(api.issues, previous)

    def test_all_pages_and_states_are_scanned_before_create(self):
        report = self.candidate_failure()
        candidate = report["issue_candidates"][0]
        api = FakeGitHub()
        api.issues = [
            {"number": i + 1, "state": "open", "body": "unrelated"} for i in range(100)
        ]
        api.issues.append(
            {
                "number": 101,
                "state": "closed",
                "title": candidate["title"],
                "body": candidate["body"],
            }
        )
        self.assertEqual(self.publish(report, api)[0]["action"], "preserved_closed")
        self.assertTrue(any("page=2" in url for _, url, _ in api.requests))
        self.assertFalse(any(method != "GET" for method, _, _ in api.requests))

    def test_duplicate_markers_pagination_and_issue_limits_refuse_all_writes(self):
        report = self.candidate_failure()
        candidate = report["issue_candidates"][0]
        api = FakeGitHub()
        api.issues = [
            {"number": i, "state": "open", "body": candidate["body"]} for i in (1, 2)
        ]
        with self.assertRaises(reporter.ContractError):
            self.publish(report, api)
        self.assertFalse(any(m != "GET" for m, _, _ in api.requests))
        api = FakeGitHub()
        api.issues = [
            {"number": i + 1, "state": "open", "body": "unrelated"} for i in range(100)
        ]
        with mock.patch.object(reporter, "MAX_ISSUE_PAGES", 1), self.assertRaises(
            reporter.ContractError
        ):
            self.publish(report, api)
        self.assertFalse(any(m != "GET" for m, _, _ in api.requests))
        api = FakeGitHub()
        with mock.patch.object(reporter, "MAX_ISSUES", 0), self.assertRaises(
            reporter.ContractError
        ):
            self.publish(report, api)
        self.assertEqual(api.requests, [])

    def test_untrusted_events_missing_context_and_wrong_target_fail_closed(self):
        report = self.candidate_failure()
        for key, value in (
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("GITHUB_EVENT_NAME", "pull_request_target"),
            ("GITHUB_REF", "refs/pull/1/merge"),
            ("GITHUB_HEAD_REF", "feature"),
            ("GITHUB_REPOSITORY", "fork/qualification"),
            ("GITHUB_RUN_ATTEMPT", "2"),
            ("GITHUB_SHA", "f" * 40),
            ("GITHUB_TOKEN", ""),
            ("GITHUB_ACTIONS", "false"),
        ):
            api = FakeGitHub()
            environment = {**self.context(), key: value}
            with self.subTest(key=key, value=value), self.assertRaises(
                reporter.ContractError
            ):
                self.publish(report, api, environment=environment)
            self.assertFalse(any(m != "GET" for m, _, _ in api.requests))
        with self.assertRaises(reporter.ContractError):
            self.publish(report, FakeGitHub(), target="")

    def test_candidate_overflow_refuses_all_publication(self):
        self.manifest["jobs"][0]["expected_tests"].append(OTHER_TEST)
        self.result("baseline", junit((TEST, "success"), (OTHER_TEST, "success")))
        self.result(
            "candidate",
            junit((TEST, "failure"), (OTHER_TEST, "failure")),
            status="failure",
        )
        with mock.patch.object(reporter, "MAX_ISSUE_CANDIDATES", 1):
            report = self.report()
        api = FakeGitHub()
        with self.assertRaises(reporter.ContractError):
            self.publish(report, api)
        self.assertEqual(api.requests, [])

    def test_persisted_issue_text_is_read_back_and_verified(self):
        report = self.candidate_failure()
        api = FakeGitHub()
        original_request = api.send_request

        def altered_readback(url: str, **kwargs) -> object:
            response = original_request(url, **kwargs)
            if kwargs.get("method", "GET") == "GET" and url.endswith("/issues/1"):
                response["body"] = "altered text"
            return response

        with mock.patch.object(
            api, "send_request", side_effect=altered_readback
        ), self.assertRaises(reporter.ContractError):
            self.publish(report, api)

    def test_retry_after_lost_create_response_finds_existing_issue(self):
        report = self.candidate_failure()
        api = FakeGitHub()
        original_request = api.send_request

        def lost_response(url: str, **kwargs) -> object:
            response = original_request(url, **kwargs)
            if kwargs.get("method") == "POST":
                raise reporter.GitHubAPIError(
                    "mock response lost after successful create"
                )
            return response

        with mock.patch.object(
            api, "send_request", side_effect=lost_response
        ), self.assertRaises(reporter.GitHubAPIError):
            self.publish(report, api)
        self.assertEqual(self.publish(report, api)[0]["action"], "unchanged")
        self.assertEqual(len(api.issues), 1)
        self.assertEqual(sum(method == "POST" for method, _, _ in api.requests), 1)

    def test_pr_manifest_event_cannot_be_promoted_by_matching_environment(self):
        for event in (
            "pull_request",
            "pull_request_target",
            "push",
            "workflow_run",
            "local",
        ):
            self.manifest["run"]["event"] = event
            if event == "local":
                self.local()
            report = self.candidate_failure()
            api = FakeGitHub()
            with self.subTest(event=event), self.assertRaises(reporter.ContractError):
                self.publish(report, api)
            self.assertEqual(api.requests, [])

    def test_spoofed_manifests_are_checked_against_api_run_identity(self):
        report = self.candidate_failure()
        for field, value in (
            ("event", "pull_request"),
            ("head_branch", "feature"),
            ("run_attempt", 2),
            ("head_sha", "f" * 40),
            ("head_repository", {"full_name": "fork/qualification"}),
            ("pull_requests", [{"number": 1}]),
        ):
            api = FakeGitHub()
            api.run[field] = value
            with self.subTest(field=field), self.assertRaises(reporter.ContractError):
                self.publish(report, api)
            self.assertFalse(any(m != "GET" for m, _, _ in api.requests))
        api = FakeGitHub()
        api.repository["fork"] = True
        with self.assertRaises(reporter.ContractError):
            self.publish(report, api)

    def test_schedule_is_supported_and_only_explicit_token_client_is_constructed(self):
        self.manifest["run"]["event"] = "schedule"
        self.candidate_failure()
        api = FakeGitHub()
        api.run["event"] = "schedule"
        output = self.root / "published"
        with mock.patch.dict(os.environ, self.context(), clear=True), mock.patch.object(
            reporter, "GitHubAPI", return_value=api
        ) as constructor:
            code = reporter.main(
                [
                    "--input",
                    str(self.path),
                    "--output-dir",
                    str(output),
                    "--publish",
                    "--issue-repository",
                    "example/burn-down",
                ]
            )
        self.assertEqual(code, 1)
        constructor.assert_called_once_with(github_token="mock-token")
        self.assertEqual(
            json.loads((output / "publication.json").read_text())["operations"][0][
                "action"
            ],
            "created",
        )

    def test_default_offline_and_explicit_target_required_before_api_construction(self):
        self.candidate_failure()
        args = ["--input", str(self.path), "--output-dir", str(self.root / "offline")]
        with mock.patch.object(reporter, "GitHubAPI") as client:
            self.assertEqual(reporter.main(args), 1)
            client.assert_not_called()
        with mock.patch.dict(os.environ, self.context(), clear=True), mock.patch.object(
            reporter, "GitHubAPI"
        ) as client, redirect_stderr(io.StringIO()):
            self.assertEqual(reporter.main(args + ["--publish"]), 2)
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
