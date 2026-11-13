#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Compare explicit ROCr/Rust HSA qualification inventories without running tools.

The manifest is versioned, rejects unknown fields, and lists expected jobs and
individual tests before collecting results. JUnit is evidence, not an inventory:
missing results cannot qualify a transition. Outputs contain structured public
identities and immutable evidence links, never XML diagnostics or local paths.

Publishing is opt-in, requires a verified default-branch Actions run, and never
closes or reopens issues. Callers must serialize publishers for a target repo;
GitHub has no atomic issue create-if-absent operation.
"""

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Mapping
import xml.etree.ElementTree as ET

from github_actions.github_actions_api import GitHubAPI, GitHubAPIError

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_JOBS = 1024
MAX_TESTS = 20_000
MAX_JUNIT_FILES = 64
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_XML_ELEMENTS = 100_000
MAX_XML_DEPTH = 32
MAX_TOTAL_XML_BYTES = 64 * 1024 * 1024
MAX_ISSUES = 100
MAX_ISSUE_CANDIDATES = 1000
MAX_ISSUE_PAGES = 100
MAX_ISSUE_BODY = 60_000
ROLES = ("baseline", "candidate")
FAILURES = {"failure", "error", "timed_out"}
STATUSES = FAILURES | {"success", "cancelled", "skipped", "infrastructure_failure"}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
REPOSITORY = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"
)
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:+,=@()\[\] /-]{0,255}")
UNSAFE = re.compile(
    r"(?:^|[\s=\[(])(?:/|[A-Za-z]:[\\/]|~/)|"
    r"(?:gh[pousr]_|github_pat_|AKIA|ASIA)[A-Za-z0-9_]+|"
    r"(?:token|password|secret|authorization|credential)\s*[:=]|"
    r"/(?:home|Users|tmp|private|var)/|file://",
    re.IGNORECASE,
)
MARKER = re.compile(r"<!-- runtime-transition:v1:([0-9a-f]{64}) -->")
SENSITIVE_FIELD = re.compile(
    r"(?:^|[_-])(?:tokens?|passwords?|secrets?|credentials?|authorization|private_key|api_key|access_key)(?:$|[_-])",
    re.IGNORECASE,
)


class ContractError(ValueError):
    """Invalid or unsafe input; messages contain no supplied values."""


class EvidenceBudgetError(ContractError):
    """The run's evidence budget is exhausted; collection must stop."""


@dataclass(frozen=True, order=True)
class TestId:
    # JUnit classname, empty when the producer omits it.
    classname: str
    # Exact JUnit name, including parameterization.
    name: str
    # Digest of the exact native pair, disambiguating redacted/public spellings.
    identity_sha256: str


@dataclass
class Observation:
    # Runner's terminal outcome, independent of JUnit outcomes.
    status: str
    # Whether the build/test command actually started.
    started: bool
    # Measured OS, kernel, driver, GPU, and container identities.
    environment: dict[str, str]
    # Evidence in the manifest's Actions run.
    evidence_url: str | None
    # Immutable source instructions for reproduction.
    reproduction_url: str | None
    # Local input paths and expected content digests; never serialized.
    junit: list[tuple[Path, str]]
    # Tests observed starting, used only to attribute a runner timeout.
    executed_tests: set[TestId]
    # Safe relative artifact/source locators for local evidence only.
    locators: dict[str, str]


@dataclass
class Job:
    # Stable matrix-cell identity, including target/platform distinctions.
    id: str
    # Build or test collection mode.
    kind: str
    # Public source component identity.
    component: str
    # Immediate predecessor job IDs.
    needs: list[str]
    # Versioned suite and selection identity, empty for builds.
    test_identity: dict[str, str]
    # Expected individual tests, independent of observed XML.
    expected_tests: set[TestId]
    # Supplied baseline/candidate observations; absence is incomplete.
    observations: dict[str, Observation]


@dataclass
class Manifest:
    # Source workflow repository; never inferred from credentials.
    repository: str
    # Exact source Actions run identity.
    run: dict[str, object]
    # Exact public provider/source/toolchain/configuration identities.
    profiles: dict[str, dict[str, object]]
    # Expected jobs, sorted in dependency order.
    jobs: list[Job]


@dataclass
class Collected:
    # Public, path-free observation and evidence metadata.
    identity: dict[str, object] = field(default_factory=dict)
    # Terminal per-test outcomes supported by evidence.
    tests: dict[TestId, str] = field(default_factory=dict)
    # Build outcome, or aggregate test-command outcome.
    outcome: str = "incomplete"
    # Structured completeness diagnostics, never raw exception text.
    reasons: set[str] = field(default_factory=set)
    # Dependencies preventing this observation from running.
    blocked_by: list[str] = field(default_factory=list)


def _object(
    value: object, required: set[str], optional: set[str] = frozenset()
) -> dict:
    if not isinstance(value, dict) or not required <= value.keys():
        raise ContractError("Missing required object fields")
    if value.keys() - required - optional:
        raise ContractError("Unknown object fields")
    return value


def _list(value: object, limit: int) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise ContractError("Invalid list or collection limit exceeded")
    return value


def _match(value: object, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ContractError("Invalid identity format")
    return value


def _public(value: object) -> str:
    text = _match(value, IDENTIFIER)
    if UNSAFE.search(text) or ".." in text or "//" in text:
        raise ContractError("Unsafe public identity")
    return text


def _identities(value: object, required: set[str]) -> dict[str, str]:
    if not isinstance(value, dict) or not required <= value.keys() or len(value) > 64:
        raise ContractError("Invalid identity map")
    result = {_public(k): _public_value(v) for k, v in value.items()}
    if any(SENSITIVE_FIELD.search(key) for key in result):
        raise ContractError("Credential fields cannot be public identities")
    return result


def _public_value(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isprintable()
        or UNSAFE.search(value)
    ):
        raise ContractError("Unsafe or invalid public value")
    return value


def _test_id(value: object) -> TestId:
    data = _object(value, {"classname", "name"})
    return _native_test_id(data["classname"], data["name"])


def _native_test_id(classname: object, name: object) -> TestId:
    names = []
    for value, empty_allowed in ((classname, True), (name, False)):
        if (
            not isinstance(value, str)
            or len(value) > 16_384
            or (not value and not empty_allowed)
        ):
            raise ContractError("Invalid or oversized native test identity")
        if value and (
            not value.isprintable() or UNSAFE.search(value) or len(value) > 2048
        ):
            value = "redacted-sha256:" + hashlib.sha256(value.encode()).hexdigest()
        names.append(value)
    digest = hashlib.sha256(
        json.dumps([classname, name], ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TestId(names[0], names[1], digest)


def _test_set(value: object) -> set[TestId]:
    items = [_test_id(item) for item in _list(value, MAX_TESTS)]
    if len(items) != len(set(items)):
        raise ContractError("Duplicate test identity")
    return set(items)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Duplicate JSON field")
        result[key] = value
    return result


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ContractError("Input byte limit exceeded")
    return data


def _relative_locator(value: object) -> str:
    locator = _match(value, re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,239}"))
    if any(part in {"", ".", ".."} for part in locator.split("/")) or UNSAFE.search(
        locator
    ):
        raise ContractError("Unsafe relative evidence locator")
    return locator


def load_manifest(path: Path) -> Manifest:
    """Validate all public identities before collecting any local artifacts."""
    try:
        decoded = json.loads(
            _read_bounded(path, MAX_MANIFEST_BYTES), object_pairs_hook=_unique_object
        )
    except ContractError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("Invalid manifest JSON") from exc
    data = _object(
        decoded,
        {"schema_version", "repository", "run", "profiles", "jobs"},
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ContractError("Unsupported schema version")
    repository = _match(data["repository"], REPOSITORY)
    run = _object(data["run"], {"id", "attempt", "event", "ref", "sha", "url"})
    _match(run["event"], re.compile(r"[a-z_]{1,64}"))
    local = run["event"] == "local"
    _match(
        run["id"],
        re.compile(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}" if local else r"[1-9][0-9]{0,19}"
        ),
    )
    if local:
        _public(run["id"])
    if type(run["attempt"]) is not int or not 1 <= run["attempt"] <= 10_000:
        raise ContractError("Invalid run attempt")
    _match(run["ref"], re.compile(r"refs/(?:heads|pull)/[A-Za-z0-9_./-]{1,200}"))
    _match(run["sha"], SHA)
    run_url = (
        None if local else f"https://github.com/{repository}/actions/runs/{run['id']}"
    )
    if run["url"] != run_url:
        raise ContractError("Run URL does not identify this run")
    profiles = _object(data["profiles"], set(ROLES))
    for role, provider in zip(ROLES, ("rocr", "rust-hsa")):
        profile = _object(
            profiles[role], {"provider", "sources", "toolchain", "configuration"}
        )
        if profile["provider"] != provider:
            raise ContractError("Unexpected provider identity")
        sources = _identities(profile["sources"], {"therock", "runtime"})
        for revision in sources.values():
            _match(revision, SHA)
        toolchain = _object(profile["toolchain"], {"id", "sha256"})
        _public_value(toolchain["id"])
        _match(toolchain["sha256"], DIGEST)
        configuration = _identities(
            profile["configuration"], {"build_type", "runtime_flag", "hip_flag"}
        )
        if any(
            configuration[key] not in {"ON", "OFF"}
            for key in ("runtime_flag", "hip_flag")
        ):
            raise ContractError("Provider flags must be explicit ON/OFF values")
    jobs = {}
    for item in _list(data["jobs"], MAX_JOBS):
        item = _object(
            item,
            {"id", "kind", "component", "needs", "observations"},
            {"test_identity", "expected_tests"},
        )
        job_id = _public(item["id"])
        if job_id in jobs or item["kind"] not in ("build", "test"):
            raise ContractError("Duplicate job or invalid job kind")
        kind = item["kind"]
        needs = [_public(v) for v in _list(item["needs"], MAX_JOBS)]
        if len(needs) != len(set(needs)):
            raise ContractError("Duplicate dependency")
        test_identity = {}
        expected_tests = set()
        if kind == "test":
            test_identity = _identities(
                item.get("test_identity"), {"suite", "revision", "configuration"}
            )
            _match(test_identity["revision"], SHA)
            expected_tests = _test_set(item.get("expected_tests"))
            if not expected_tests:
                raise ContractError("Expected test inventory must be nonempty")
        elif "test_identity" in item or "expected_tests" in item:
            raise ContractError("Build job contains test fields")
        observations = _object(item["observations"], set(), set(ROLES))
        parsed = {}
        for role, value in observations.items():
            value = _object(
                value,
                {
                    "status",
                    "started",
                    "environment",
                    "evidence_url",
                    "reproduction_url",
                },
                ({"junit", "executed_tests"} if kind == "test" else set())
                | ({"evidence_locator", "reproduction_locator"} if local else set()),
            )
            if (
                not isinstance(value["status"], str)
                or value["status"] not in STATUSES - {"error"}
                or type(value["started"]) is not bool
            ):
                raise ContractError("Invalid observation status or started flag")
            environment = _identities(
                value["environment"], {"os", "kernel", "driver", "gpu", "container"}
            )
            _match(environment["container"], re.compile(r"sha256:[0-9a-f]{64}"))
            evidence = value["evidence_url"]
            if local:
                if evidence is not None:
                    raise ContractError(
                        "Local evidence must not claim a public Actions URL"
                    )
            else:
                evidence = _match(
                    evidence,
                    re.compile(
                        re.escape(run_url) + r"/(?:job|artifacts)/[1-9][0-9]{0,19}"
                    ),
                )
            reproduction = value["reproduction_url"]
            if not (local and reproduction is None):
                reproduction = _match(
                    reproduction,
                    re.compile(
                        r"https://github\.com/"
                        + re.escape(repository)
                        + r"/blob/"
                        + SHA.pattern
                        + r"/[A-Za-z0-9_./-]{1,240}"
                    ),
                )
                if "/../" in reproduction or "/./" in reproduction:
                    raise ContractError("Unsafe reproduction URL")
            locators = {
                key: _relative_locator(value[key])
                for key in ("evidence_locator", "reproduction_locator")
                if key in value
            }
            junit = []
            seen_paths = set()
            for artifact in _list(value.get("junit", []), MAX_JUNIT_FILES):
                artifact = _object(artifact, {"path", "sha256"})
                local_path = artifact["path"]
                if (
                    not isinstance(local_path, str)
                    or not local_path
                    or len(local_path) > 4096
                ):
                    raise ContractError("Invalid local artifact path")
                resolved = (path.parent / local_path).resolve()
                if resolved in seen_paths:
                    raise ContractError("Duplicate JUnit input")
                seen_paths.add(resolved)
                junit.append((resolved, _match(artifact["sha256"], DIGEST)))
            executed = _test_set(value.get("executed_tests", []))
            if not executed <= expected_tests:
                raise ContractError("Started test is outside expected inventory")
            parsed[role] = Observation(
                value["status"],
                value["started"],
                environment,
                evidence,
                reproduction,
                junit,
                executed,
                locators,
            )
        jobs[job_id] = Job(
            job_id,
            kind,
            _public(item["component"]),
            sorted(needs),
            test_identity,
            expected_tests,
            parsed,
        )
    if not jobs:
        raise ContractError("Expected job inventory must be nonempty")
    ordered = []
    completed = set()
    pending = dict(jobs)
    while pending:
        ready = sorted(
            key for key, job in pending.items() if set(job.needs) <= completed
        )
        if not ready:
            raise ContractError("Unknown dependency or dependency cycle")
        ordered.extend(pending.pop(key) for key in ready)
        completed.update(ready)
    return Manifest(repository, run, profiles, ordered)


def parse_junit(data: bytes) -> dict[TestId, str]:
    """Parse pytest/gtest/CTest JUnit; reject counters that hide absent tests."""
    if len(data) > MAX_XML_BYTES:
        raise ContractError("JUnit byte limit exceeded")
    try:
        text = data.decode("utf-8-sig")
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
            raise ContractError("DTD or non-UTF8 JUnit is unsupported")
        depth = 0
        count = 0
        parser = ET.iterparse(io.StringIO(text), events=("start", "end"))
        for event, element in parser:
            if event == "start":
                depth += 1
                count += 1
                if depth > MAX_XML_DEPTH or count > MAX_XML_ELEMENTS:
                    raise ContractError("JUnit structure limit exceeded")
            else:
                depth -= 1
        root = parser.root
    except (UnicodeError, ET.ParseError) as exc:
        raise ContractError("Malformed JUnit") from exc
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag not in {"testsuite", "testsuites"}:
        raise ContractError("Unsupported JUnit root")
    results = {}
    elements = list(root.iter("testcase"))
    if len(elements) > MAX_TESTS:
        raise ContractError("JUnit test limit exceeded")
    outcomes = {}
    parents = {id(child): parent.tag for parent in root.iter() for child in parent}
    for case in elements:
        if parents.get(id(case)) != "testsuite":
            raise ContractError("JUnit testcase outside a testsuite")
        identity = _native_test_id(case.get("classname", ""), case.get("name"))
        if identity in results:
            raise ContractError("Duplicate JUnit test identity")
        if any(
            c.tag
            not in {
                "failure",
                "error",
                "skipped",
                "system-out",
                "system-err",
                "properties",
            }
            for c in case
        ):
            raise ContractError("Unsupported JUnit testcase result")
        terminal = [c.tag for c in case if c.tag in {"failure", "error", "skipped"}]
        if (
            len(set(terminal)) > 1
            or case.get("status", "run") not in {"run", "notrun"}
            or case.get("result", "completed")
            not in {"completed", "skipped", "suppressed"}
        ):
            raise ContractError("Contradictory JUnit testcase result")
        outcome = terminal[0] if terminal else "success"
        if case.get("status") == "notrun" or case.get("result") in {
            "skipped",
            "suppressed",
        }:
            if terminal and terminal != ["skipped"]:
                raise ContractError("Unexecuted JUnit failure")
            outcome = "skipped"
        results[identity] = outcome
        outcomes[id(case)] = outcome
    for suite in root.iter():
        if suite.tag not in {"testsuite", "testsuites"}:
            continue
        if suite is not root and parents.get(id(suite)) not in {
            "testsuite",
            "testsuites",
        }:
            raise ContractError("JUnit suite outside the suite hierarchy")
        allowed = {"testsuite", "properties", "system-out", "system-err"}
        if suite.tag == "testsuite":
            allowed.add("testcase")
        if any(child.tag not in allowed for child in suite):
            raise ContractError("Unsupported JUnit suite result")
        counts = Counter(outcomes[id(case)] for case in suite.iter("testcase"))
        expected = {
            "tests": sum(counts.values()),
            "failures": counts["failure"],
            "errors": counts["error"],
            "skipped": counts["skipped"],
        }
        for name, count in expected.items():
            if name in suite.attrib and suite.attrib[name] != str(count):
                raise ContractError("JUnit counters contradict individual results")
    return results


def _profiles_comparable(manifest: Manifest) -> bool:
    baseline, candidate = (manifest.profiles[role] for role in ROLES)

    def comparable_identity(profile: dict[str, object]) -> dict[str, object]:
        return {
            "sources": {k: v for k, v in profile["sources"].items() if k != "runtime"},
            "toolchain": profile["toolchain"],
            "configuration": {
                k: v for k, v in profile["configuration"].items() if k != "runtime_flag"
            },
        }

    return (
        comparable_identity(baseline) == comparable_identity(candidate)
        and baseline["sources"]["therock"] == manifest.run["sha"]
        and baseline["configuration"]["runtime_flag"] == "OFF"
        and candidate["configuration"]["runtime_flag"] == "ON"
    )


def _read_junit(path: Path, budget: list[int]) -> bytes:
    remaining = MAX_TOTAL_XML_BYTES - budget[0]
    try:
        data = _read_bounded(path, min(MAX_XML_BYTES, remaining))
    except ContractError as exc:
        if remaining < MAX_XML_BYTES:
            raise EvidenceBudgetError("Run JUnit byte limit exceeded") from exc
        raise
    budget[0] += len(data)
    return data


def _collect(
    job: Job,
    observation: Observation | None,
    budget: list[int],
    *,
    blocked_by: list[str],
) -> Collected:
    result = Collected(blocked_by=blocked_by)
    if blocked_by:
        result.reasons.add("blocked_dependency")
    if observation is None:
        result.reasons.add("missing_observation")
        return result
    result.identity = {
        "status": observation.status,
        "started": observation.started,
        "environment": observation.environment,
        "evidence_url": observation.evidence_url,
        "reproduction_url": observation.reproduction_url,
        "junit": [],
        "executed_tests": [asdict(t) for t in sorted(observation.executed_tests)],
        **observation.locators,
    }
    if blocked_by:
        return result
    if not observation.started:
        result.reasons.add("command_not_started")
    if observation.status in {"cancelled", "skipped", "infrastructure_failure"}:
        result.reasons.add(observation.status)
    if job.kind == "build":
        if not result.reasons:
            result.outcome = observation.status
        return result
    artifacts_valid = True
    for path, digest in sorted(observation.junit, key=lambda p: p[1]):
        artifact = {"sha256": digest, "valid": False}
        result.identity["junit"].append(artifact)
        try:
            data = _read_junit(path, budget)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ContractError("JUnit digest mismatch")
            tests = parse_junit(data)
            if result.tests.keys() & tests.keys():
                raise ContractError("Duplicate test across JUnit files")
            if len(result.tests) + len(tests) > MAX_TESTS:
                raise ContractError("Combined JUnit test limit exceeded")
            result.tests.update(tests)
            artifact["valid"] = True
            artifact["counts"] = dict(sorted(Counter(tests.values()).items()))
        except OSError:
            result.reasons.add("unreadable_junit")
            artifacts_valid = False
        except EvidenceBudgetError:
            raise
        except ContractError:
            result.reasons.add("invalid_junit")
            artifacts_valid = False
    if not artifacts_valid:
        # A partial parse can otherwise misattribute duplicate/retried or stale tests.
        result.tests.clear()
    if result.reasons & {
        "command_not_started",
        "cancelled",
        "skipped",
        "infrastructure_failure",
    }:
        result.tests.clear()
    if artifacts_valid and observation.started and observation.status == "timed_out":
        running = observation.executed_tests - result.tests.keys()
        if len(running) == 1:
            result.tests[next(iter(running))] = "timed_out"
        elif not any(v in FAILURES for v in result.tests.values()):
            result.reasons.add("unattributed_timeout")
    if not result.tests:
        result.reasons.add("zero_tests")
    if result.tests.keys() - job.expected_tests:
        result.reasons.add("unexpected_tests")
        result.tests.clear()
    if job.expected_tests - result.tests.keys():
        result.reasons.add("missing_tests")
    if "skipped" in result.tests.values():
        result.reasons.add("skipped_tests")
    failed = any(value in FAILURES for value in result.tests.values())
    if (observation.status == "success" and failed) or (
        observation.status == "failure" and not failed
    ):
        result.reasons.add("command_junit_disagreement")
        result.tests.clear()
    if not result.reasons:
        result.outcome = "failure" if failed else "success"
    return result


def _classify(baseline: str, candidate: str, comparable: bool) -> str:
    if not comparable:
        return "incomplete"
    if baseline in FAILURES and candidate in FAILURES:
        return "shared"
    if baseline == "success" and candidate in FAILURES:
        return "candidate_only"
    if baseline in FAILURES and candidate == "success":
        return "baseline_only"
    if baseline == candidate == "success":
        return "pass"
    return "incomplete"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _issue(
    manifest: Manifest,
    job: Job,
    finding: dict[str, object],
    results: dict[str, Collected],
) -> dict[str, object]:
    # Revision/run/diagnostic changes must not create new issues for the same test.
    environment = results["candidate"].identity["environment"]
    identity = {
        "repository": manifest.repository,
        "providers": [manifest.profiles[r]["provider"] for r in ROLES],
        "job": job.id,
        "component": job.component,
        "kind": job.kind,
        "matrix": {
            "os": environment["os"],
            "gpu": environment["gpu"],
            "gpu_count": environment.get("gpu_count", "unspecified"),
            "build_type": manifest.profiles["candidate"]["configuration"]["build_type"],
        },
        "suite": {
            k: v
            for k, v in job.test_identity.items()
            if k not in {"revision", "implementation_revision"}
        },
        "test": finding.get("test"),
    }
    key = hashlib.sha256(_json(identity).encode()).hexdigest()
    marker = f"<!-- runtime-transition:v1:{key} -->"
    # Test names are exact in the structured report. A short hash keeps titles
    # bounded even for large parameterized names and avoids Markdown injection.
    title = f"[Runtime transition] {job.component[:64]} {job.kind} failure ({key[:12]})"
    body = [
        marker,
        "Runtime qualification observed a measured failure.",
        f"Classification: `{finding['classification']}`.",
        "Identity:",
        "```json",
        _json(identity).rstrip(),
        "```",
        "Source, toolchain, and provider configuration:",
        "```json",
        _json(manifest.profiles).rstrip(),
        "```",
        "Test suite identity:",
        "```json",
        _json(job.test_identity).rstrip(),
        "```",
        "Evidence and reproduction:",
    ]
    for role in ROLES:
        measured = results[role].identity
        evidence = (
            f"[evidence]({measured['evidence_url']})"
            if measured["evidence_url"]
            else "local evidence"
        )
        reproduction = (
            f"[reproduction]({measured['reproduction_url']})"
            if measured["reproduction_url"]
            else "local reproduction"
        )
        body.extend(
            [
                f"- {role}: `{finding['outcomes'][role]}`; {evidence}; {reproduction}.",
                "```json",
                _json(
                    {"environment": measured["environment"], "junit": measured["junit"]}
                ).rstrip(),
                "```",
            ]
        )
    body.append(
        f"Run attempt: {manifest.run['attempt']}. Paired job coverage complete: {str(all(not r.reasons for r in results.values())).lower()}."
    )
    if manifest.run["event"] == "local":
        body.append(
            "Local qualification: no public Actions evidence is available. This issue draft cannot be published."
        )
    public_body = "\n\n".join(body) + "\n"
    if len(public_body) > MAX_ISSUE_BODY:
        raise ContractError("Generated issue body limit exceeded")
    return {
        "dedupe_key": key,
        "title": title,
        "body": public_body,
        "finding": finding,
        "publishable": manifest.run["event"] in {"schedule", "workflow_dispatch"},
    }


def build_report(manifest: Manifest) -> dict[str, object]:
    """Collect, compare, and derive stable issue candidates without network I/O."""
    profiles_comparable = _profiles_comparable(manifest)
    collected = {}
    jobs = []
    findings = []
    candidates = []
    candidate_overflow = 0
    budget = [0]
    for job in manifest.jobs:
        results = {
            role: _collect(
                job,
                job.observations.get(role),
                budget,
                blocked_by=[
                    dep
                    for dep in job.needs
                    if collected[dep][role].outcome != "success"
                ],
            )
            for role in ROLES
        }
        collected[job.id] = results
        environments_present = all(r.identity for r in results.values())
        environment_mismatch = (
            environments_present
            and results["baseline"].identity["environment"]
            != results["candidate"].identity["environment"]
        )
        comparable = (
            profiles_comparable and environments_present and not environment_mismatch
        )
        if not profiles_comparable or environment_mismatch:
            for result in results.values():
                result.reasons.add("identity_mismatch")
        units = sorted(job.expected_tests) if job.kind == "test" else [None]
        for test in units:
            outcomes = {
                role: (result.tests.get(test, "incomplete") if test else result.outcome)
                for role, result in results.items()
            }
            classification = _classify(
                outcomes["baseline"], outcomes["candidate"], comparable
            )
            if classification == "pass":
                continue
            finding = {
                "job": job.id,
                "classification": classification,
                "outcomes": outcomes,
            }
            if test:
                finding["test"] = asdict(test)
            findings.append(finding)
            if classification in {"candidate_only", "baseline_only", "shared"}:
                if len(candidates) < MAX_ISSUE_CANDIDATES:
                    candidates.append(_issue(manifest, job, finding, results))
                else:
                    candidate_overflow += 1
        if any(result.reasons for result in results.values()):
            findings.append(
                {
                    "job": job.id,
                    "classification": "incomplete",
                    "reasons": {role: sorted(r.reasons) for role, r in results.items()},
                }
            )
        observations = {}
        for role, result in results.items():
            observations[role] = {
                **result.identity,
                "outcome": result.outcome,
                "coverage_reasons": sorted(result.reasons),
                "blocked_by": result.blocked_by,
                "tests": [
                    {**asdict(test), "outcome": outcome}
                    for test, outcome in sorted(result.tests.items())
                ],
            }
        jobs.append(
            {
                "id": job.id,
                "kind": job.kind,
                "component": job.component,
                "needs": job.needs,
                "test_identity": job.test_identity,
                "expected_tests": [asdict(t) for t in sorted(job.expected_tests)],
                "observations": observations,
            }
        )
    has_test_jobs = any(job.kind == "test" for job in manifest.jobs)
    if not has_test_jobs:
        findings.append(
            {
                "scope": "run",
                "classification": "incomplete",
                "reasons": ["no_test_jobs"],
            }
        )
    comparable = (
        has_test_jobs
        and profiles_comparable
        and all(
            not r.reasons for results in collected.values() for r in results.values()
        )
    )
    return {
        "schema_version": 1,
        "repository": manifest.repository,
        "run": manifest.run,
        "profiles": manifest.profiles,
        "comparable": comparable,
        "qualified": comparable and not findings,
        "jobs": jobs,
        "findings": findings,
        "issue_candidates": sorted(candidates, key=lambda v: v["dedupe_key"]),
        "issue_candidates_overflow": candidate_overflow,
    }


def write_report(report: dict[str, object], output_dir: Path) -> None:
    """Write reproducible artifacts; no clock readings or private paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(_json(report), encoding="utf-8")
    (output_dir / "issue-candidates.json").write_text(
        _json(
            {
                "schema_version": 1,
                "issue_candidates": report["issue_candidates"],
                "issue_candidates_overflow": report["issue_candidates_overflow"],
            }
        ),
        encoding="utf-8",
    )
    counts = Counter(f["classification"] for f in report["findings"])
    lines = [
        "# Runtime transition qualification",
        f"Run: [{report['run']['id']}]({report['run']['url']}), attempt {report['run']['attempt']}.",
        f"Qualified: **{str(report['qualified']).lower()}**. Comparable coverage: **{str(report['comparable']).lower()}**.",
        "| Classification | Findings |",
        "| --- | ---: |",
    ]
    if report["run"]["event"] == "local":
        lines[1] = (
            f"Local run {report['run']['id']}, attempt {report['run']['attempt']}. Evidence is local; no public Actions links are available."
        )
    lines.extend(
        f"| {classification} | {counts[classification]} |"
        for classification in (
            "candidate_only",
            "baseline_only",
            "shared",
            "incomplete",
        )
    )
    lines.extend(
        [
            "",
            f"Issue candidates: {len(report['issue_candidates'])}; overflow: {report['issue_candidates_overflow']}. Historical issues are never closed automatically.",
            "See `report.json` for exact identities, per-test outcomes, and coverage reasons.",
        ]
    )
    lines.extend(
        [
            "",
            "| Job | Baseline | Candidate | Coverage gaps |",
            "| --- | --- | --- | --- |",
        ]
    )
    for job in report["jobs"]:
        outcomes = []
        reasons = set()
        for role in ROLES:
            observation = job["observations"][role]
            outcome = observation["outcome"]
            url = observation.get("evidence_url")
            locator = observation.get("evidence_locator")
            if url:
                outcomes.append(f"[{outcome}]({url})")
            elif report["run"]["event"] == "local":
                outcomes.append(
                    f"{outcome} (local evidence{': ' + locator if locator else ''})"
                )
            else:
                outcomes.append(outcome)
            reasons.update(observation["coverage_reasons"])
        lines.append(
            f"| `{job['id']}` | {outcomes[0]} | {outcomes[1]} | {', '.join(sorted(reasons)) or 'none'} |"
        )
    (output_dir / "summary.md").write_text(
        "\n\n".join(lines[:3]) + "\n\n" + "\n".join(lines[3:]) + "\n", encoding="utf-8"
    )


def _verify_publish_context(
    manifest: Manifest, environment: Mapping[str, str], api: GitHubAPI
) -> None:
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": manifest.repository,
        "GITHUB_RUN_ID": manifest.run["id"],
        "GITHUB_RUN_ATTEMPT": str(manifest.run["attempt"]),
        "GITHUB_SHA": manifest.run["sha"],
        "GITHUB_EVENT_NAME": manifest.run["event"],
        "GITHUB_REF": manifest.run["ref"],
    }
    if manifest.run["event"] not in {"schedule", "workflow_dispatch"} or any(
        environment.get(k) != v for k, v in expected.items()
    ):
        raise ContractError("Publication requires a matching trusted Actions context")
    if environment.get("GITHUB_HEAD_REF") or not environment.get("GITHUB_TOKEN"):
        raise ContractError("Publication requires a token and excludes PR contexts")
    base = f"https://api.github.com/repos/{manifest.repository}"
    repository = api.send_request(base)
    if (
        not isinstance(repository, dict)
        or repository.get("full_name") != manifest.repository
        or repository.get("fork") is not False
    ):
        raise ContractError("Publication requires the source repository, not a fork")
    if manifest.run["ref"] != f"refs/heads/{repository.get('default_branch')}":
        raise ContractError("Publication requires the default branch")
    run = api.send_request(f"{base}/actions/runs/{manifest.run['id']}")
    if not isinstance(run, dict) or any(
        run.get(k) != v
        for k, v in {
            "id": int(manifest.run["id"]),
            "run_attempt": manifest.run["attempt"],
            "event": manifest.run["event"],
            "head_sha": manifest.run["sha"],
            "head_branch": repository.get("default_branch"),
            "html_url": manifest.run["url"],
        }.items()
    ):
        raise ContractError("Publication run does not match verified metadata")
    head_repository = run.get("head_repository")
    if (
        not isinstance(head_repository, dict)
        or head_repository.get("full_name") != manifest.repository
        or run.get("pull_requests") != []
    ):
        raise ContractError("Publication excludes fork and pull request runs")


def publish_issues(
    manifest: Manifest,
    report: dict[str, object],
    *,
    issue_repository: str,
    environment: Mapping[str, str],
    api: GitHubAPI,
) -> list[dict[str, object]]:
    """Idempotently upsert open owned issues. Preserve closed and absent issues.

    All issue pages are checked before any write. Pagination/duplicate/overflow
    failures cannot turn an incomplete scan into duplicate issue creation.
    """
    _match(issue_repository, REPOSITORY)
    candidates = report["issue_candidates"]
    if len(candidates) > MAX_ISSUES or report["issue_candidates_overflow"]:
        raise ContractError("Issue publication limit exceeded")
    if any(not candidate.get("publishable") for candidate in candidates):
        raise ContractError("Issue candidates lack publishable public evidence")
    _verify_publish_context(manifest, environment, api)
    base = f"https://api.github.com/repos/{issue_repository}/issues"
    owned = {}
    for page in range(1, MAX_ISSUE_PAGES + 1):
        issues = api.send_request(f"{base}?state=all&per_page=100&page={page}")
        if not isinstance(issues, list) or len(issues) > 100:
            raise ContractError("Invalid issue listing")
        for issue in issues:
            if not isinstance(issue, dict):
                raise ContractError("Invalid issue listing entry")
            if "pull_request" in issue:
                continue
            body = issue.get("body") or ""
            if not isinstance(body, str):
                raise ContractError("Invalid issue body")
            keys = MARKER.findall(body)
            if len(keys) > 1 or any(key in owned for key in keys):
                raise ContractError("Duplicate owned issue markers")
            if keys:
                if (
                    type(issue.get("number")) is not int
                    or issue["number"] <= 0
                    or issue.get("state") not in {"open", "closed"}
                ):
                    raise ContractError("Invalid owned issue metadata")
                owned[keys[0]] = issue
        if len(issues) < 100:
            break
    else:
        raise ContractError("Issue pagination limit exceeded")
    operations = []
    for candidate in candidates:
        issue = owned.get(candidate["dedupe_key"])
        body = {"title": candidate["title"], "body": candidate["body"]}
        if issue and issue["state"] == "closed":
            operations.append(
                {
                    "dedupe_key": candidate["dedupe_key"],
                    "action": "preserved_closed",
                    "number": issue["number"],
                }
            )
            continue
        if issue and all(issue.get(k) == v for k, v in body.items()):
            operations.append(
                {
                    "dedupe_key": candidate["dedupe_key"],
                    "action": "unchanged",
                    "number": issue["number"],
                }
            )
            continue
        response = api.send_request(
            f"{base}/{issue['number']}" if issue else base,
            method="PATCH" if issue else "POST",
            body=body,
        )
        if (
            not isinstance(response, dict)
            or type(response.get("number")) is not int
            or response["number"] <= 0
            or (issue and response["number"] != issue["number"])
        ):
            raise ContractError("Issue write returned invalid metadata")
        # Read back the persisted artifact, rather than trusting a write response.
        persisted = api.send_request(f"{base}/{response['number']}")
        if not isinstance(persisted, dict) or any(
            persisted.get(k) != v for k, v in body.items()
        ):
            raise ContractError("Issue round-trip verification failed")
        operations.append(
            {
                "dedupe_key": candidate["dedupe_key"],
                "action": "updated" if issue else "created",
                "number": response["number"],
            }
        )
    return operations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="Version 1 qualification manifest"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Upsert issues in a verified trusted Actions context",
    )
    parser.add_argument(
        "--issue-repository",
        help="Explicit OWNER/REPO write target; required for --publish",
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.input)
        report = build_report(manifest)
        write_report(report, args.output_dir)
        if args.publish:
            if not args.issue_repository or not os.environ.get("GITHUB_TOKEN"):
                raise ContractError(
                    "Publishing requires an explicit issue repository and token"
                )
            operations = publish_issues(
                manifest,
                report,
                issue_repository=args.issue_repository,
                environment=os.environ,
                api=GitHubAPI(github_token=os.environ["GITHUB_TOKEN"]),
            )
            (args.output_dir / "publication.json").write_text(
                _json({"schema_version": 1, "operations": operations}), encoding="utf-8"
            )
        return 0 if report["qualified"] else 1
    except (
        ContractError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        GitHubAPIError,
    ) as exc:
        # Never print raw XML, credentials, local paths, or API response bodies.
        print(
            f"runtime transition report refused: {exc if isinstance(exc, ContractError) else type(exc).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
