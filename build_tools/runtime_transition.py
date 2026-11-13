#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Build paired, isolated distributions and measure a pinned test selection.

Build and test commands produce local reports and detailed diagnostics. The
container command reproduces CI locally; dispatch starts an approved worker.
Explicit publish-report writes bounded issues after trusted default-branch
Actions context is verified.
"""

import argparse
import ctypes
from dataclasses import asdict, dataclass, field
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tomllib
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from github_actions.detect_failed_logs import find_failed_logs
import runtime_transition_snapshot as snapshot

SCRIPT_DIR = Path(__file__).resolve().parent
SUPPORT_DIR = SCRIPT_DIR / "runtime_transition"
DEFAULT_RUNTIME = "e38b188430ea50e7fd735364700b09a429115dd3"
SHA = re.compile(r"[0-9a-f]{40}")
IMAGE = re.compile(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}")
LOCAL_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY = re.compile(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+")
PROVIDERS = {"baseline": "rocr", "candidate": "rust-hsa"}
STAGES = {"baseline": "ROCR-Runtime", "candidate": "rust-hsa"}
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
COMPONENT_SCRIPTS = {"test_runner.py", "test_hiptests.py"}
GPU_CONTAINER_OPTIONS = [
    "--ipc=host",
    "--security-opt",
    "seccomp=unconfined",
    "--ulimit",
    "memlock=-1:-1",
    "--ulimit",
    "nofile=1048576:1048576",
]
BUILD_TOOLS = (
    "cmake",
    "ninja",
    "git",
    "cc",
    "c++",
    "make",
    "pkg-config",
    "patch",
    "curl",
    "tar",
    "xz",
    "autoconf",
    "automake",
    "bison",
    "flex",
    "gfortran",
    "libtoolize",
    "makeinfo",
    "patchelf",
    "node",
    "npm",
    "cargo",
    "rustc",
    "rustfmt",
    "cargo-fmt",
)
FOUNDATION_TESTS = [
    {"classname": "hsa", "name": name} for name in ("init", "enumerate-gpu", "shutdown")
]


class PrerequisiteError(ValueError):
    """An absent prerequisite cannot be attributed to runtime behavior."""


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


@dataclass
class Suite:
    # Stable reporter/job identity.
    id: str
    # Public component identity.
    component: str
    # ctest, component (existing script), or gtest.
    kind: str
    # Directory or executable relative to the build tree.
    path: str = "."
    # Existing TheRock script basename (component kind only).
    script: str = "test_runner.py"
    # Existing script's test category; never silently falls back to quick.
    category: str = "full"
    # Explicit optional CTest label selection.
    label: str = ""
    # Optional CTest exclusion, recorded in the plan identity.
    exclude_label: str = ""
    # Per-suite outer timeout, including script setup.
    timeout: int = 14400


@dataclass
class Plan:
    # Content hash, included in comparison configuration.
    sha256: str
    # Explicit corpus selection.
    suites: list[Suite]
    # Fixed configure options shared by both providers.
    cmake: dict[str, str]


def load_plan(path: Path) -> Plan:
    data = json.loads(path.read_text())
    if (
        set(data) != {"schema_version", "suites", "cmake"}
        or data["schema_version"] != 1
    ):
        raise ValueError("Expected plan schema_version 1 with suites and cmake")
    suites = [Suite(**item) for item in data["suites"]]
    if not suites or len(suites) > 100 or len({s.id for s in suites}) != len(suites):
        raise ValueError("Plan requires 1..100 uniquely named suites")
    for suite in suites:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", suite.id):
            raise ValueError("Invalid suite id")
        if suite.kind not in {"ctest", "component", "gtest"}:
            raise ValueError(
                "Tests must use CTest, GTest, or an existing component script"
            )
        if Path(suite.path).is_absolute() or ".." in Path(suite.path).parts:
            raise ValueError("Suite paths must stay within the build tree")
        if suite.script not in COMPONENT_SCRIPTS:
            raise ValueError(
                "Unsupported component script; add an inventory-aware adapter"
            )
        if suite.category not in {"quick", "standard", "comprehensive", "full"}:
            raise ValueError("Invalid component test category")
        if not 1 <= suite.timeout <= 86400:
            raise ValueError("Invalid suite timeout")
    cmake = data["cmake"]
    if not isinstance(cmake, dict) or any(
        not re.fullmatch(r"THEROCK_(?:ENABLE|BUILD)_[A-Z0-9_]+", key)
        or value not in {"ON", "OFF"}
        for key, value in cmake.items()
    ):
        raise ValueError(
            "Plan CMake options must be boolean THEROCK_ENABLE/BUILD options"
        )
    if any(
        key in cmake
        for key in (
            "THEROCK_ENABLE_ROCM_RUNTIMES",
            "THEROCK_ENABLE_ALL",
            "THEROCK_BUILD_TESTING",
        )
    ):
        raise ValueError("Plan cannot override paired full-build invariants")
    return Plan(digest(path), suites, cmake)


def clean_environment(root: Path | None = None) -> dict[str, str]:
    # Allowlist prevents inherited loader, compiler, source, and test filters.
    environment = {
        key: os.environ[key]
        for key in ("HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    environment.update(
        PATH=SYSTEM_PATH,
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
    )
    for key in ("CARGO_HOME", "RUSTUP_HOME"):
        value = os.environ.get(key, "")
        if value.startswith("/opt/runtime-transition/"):
            environment[key] = value
    if root is not None:
        environment["PATH"] = f"{root / 'bin'}:{root / 'lib/llvm/bin'}:{SYSTEM_PATH}"
        environment["LD_LIBRARY_PATH"] = ":".join(
            str(root / path) for path in ("lib", "lib/llvm/lib", "lib/rocm_sysdeps/lib")
        )
        for key in ("ROCM_PATH", "ROCM_ROOT", "ROCM_HOME", "HIP_PATH"):
            environment[key] = str(root)
        environment.update(
            THEROCK_BIN_DIR=str(root / "bin"),
            OUTPUT_ARTIFACTS_DIR=str(root),
            SHARD_INDEX="1",
            TOTAL_SHARDS="1",
        )
    return environment


@dataclass
class CommandResult:
    # Terminal measured or infrastructure status.
    status: str
    # Whether a child was actually spawned.
    started: bool


class Runner:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.sequence = 0

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        output: Path | None = None,
    ) -> CommandResult:
        self.sequence += 1
        prefix = self.directory / f"command-{self.sequence:04d}"
        write_json(prefix.with_suffix(".json"), {"command": command, "cwd": str(cwd)})
        with (output or prefix.with_suffix(".stdout")).open(
            "w"
        ) as stdout, prefix.with_suffix(".stderr").open("w") as stderr:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError as error:
                stderr.write(str(error))
                return CommandResult("infrastructure_failure", False)
            try:
                status = "success" if process.wait(timeout=timeout) == 0 else "failure"
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                status = "timed_out"
            except KeyboardInterrupt:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
        return CommandResult(status, True)


def capture(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    return subprocess.check_output(command, cwd=cwd, env=env, text=True).strip()


def source_identity(source: Path, environment: dict[str, str]) -> dict[str, str]:
    identities = {
        "therock": capture(["git", "rev-parse", "HEAD"], cwd=source, env=environment)
    }
    # Record indexed source pins; deterministic patch contents belong to the
    # pinned TheRock tree. git-am committer timestamps are not source changes.
    for line in capture(
        ["git", "ls-files", "--stage"], cwd=source, env=environment
    ).splitlines():
        fields = line.split()
        if fields[0] == "160000":
            identities[fields[3]] = fields[1]
    for line in capture(
        ["git", "submodule", "status", "--recursive"], cwd=source, env=environment
    ).splitlines():
        if line.startswith("U"):
            raise PrerequisiteError("Submodule snapshot contains a merge conflict")
    identities["runtime"] = capture(
        ["git", "rev-parse", "HEAD"], cwd=source / "rocm-systems", env=environment
    )
    return identities


def verify_build_tools() -> None:
    missing = [
        tool for tool in BUILD_TOOLS if shutil.which(tool, path=SYSTEM_PATH) is None
    ]
    if missing:
        raise PrerequisiteError("Missing build prerequisites: " + ", ".join(missing))


def hardware_identity(expected_gpu: str, container: str) -> dict[str, str]:
    verify_build_tools()
    if any(Path("/opt").glob("rocm*")):
        raise PrerequisiteError("Container must contain no system ROCm installation")
    nodes = Path("/sys/class/kfd/kfd/topology/nodes")
    architectures = []
    for properties in sorted(nodes.glob("*/properties")):
        values = dict(
            line.split(maxsplit=1)
            for line in properties.read_text().splitlines()
            if " " in line
        )
        version = int(values.get("gfx_target_version", "0"))
        if version:
            architectures.append(
                f"gfx{version // 10000}{version // 100 % 100}{version % 100:x}"
            )
    if not architectures or set(architectures) != {expected_gpu}:
        raise PrerequisiteError("Requested GPU does not match the exposed KFD topology")
    if not os.access("/dev/kfd", os.R_OK | os.W_OK):
        raise PrerequisiteError("KFD device is not accessible")
    driver = Path("/sys/module/amdgpu/srcversion")
    if not driver.is_file():
        raise PrerequisiteError("AMDGPU driver identity is unavailable")
    release = platform.freedesktop_os_release()
    return {
        "os": f"{release['ID']}-{release['VERSION_ID']}",
        "kernel": platform.release(),
        "driver": driver.read_text().strip(),
        "gpu": f"{expected_gpu}-count{len(architectures)}",
        "container": container.rsplit("@", 1)[-1],
    }


def validate_image_provenance(
    metadata: dict[str, object], provenance: Path
) -> dict[str, object]:
    labels = metadata["Labels"]
    data = json.loads(provenance.read_text())
    if metadata.get("RuntimeOptions") != GPU_CONTAINER_OPTIONS:
        raise PrerequisiteError(
            "Inspected invocation lacks the required GPU container options"
        )
    for field, suffix in (
        ("parent_image", "parent-image"),
        ("runtime_ref", "runtime-ref"),
        ("cargo_lock_sha256", "cargo-lock-sha256"),
    ):
        if data[field] != labels.get(f"org.therock.runtime-transition.{suffix}"):
            raise PrerequisiteError(
                "Inspected image labels and installed provisioning metadata differ"
            )
    if data["downloads"]["rust_version"] != labels.get(
        "org.therock.runtime-transition.rust-toolchain"
    ):
        raise PrerequisiteError(
            "Installed Rust toolchain provenance differs from the image label"
        )
    return data


def ctest_inventory(path: Path, suite: str) -> list[dict[str, str]]:
    data = json.loads(path.read_text())
    if data.get("kind") != "ctestInfo" or not data.get("tests"):
        raise PrerequisiteError("CTest inventory is empty or malformed")
    tests = [{"classname": suite, "name": test["name"]} for test in data["tests"]]
    if len({test["name"] for test in tests}) != len(tests):
        raise PrerequisiteError("Duplicate CTest identity")
    return tests


def gtest_inventory(path: Path) -> list[dict[str, str]]:
    tests = []
    classname = ""
    for line in path.read_text().splitlines():
        text = line.split("#", 1)[0].rstrip()
        if not text:
            continue
        if not text.startswith(" ") and text.endswith("."):
            classname = text[:-1]
        elif text.startswith("  ") and classname:
            name = text.strip()
            # Native GTest leaves disabled tests unexecuted by default.
            if "DISABLED_" not in classname and not name.startswith("DISABLED_"):
                tests.append({"classname": classname, "name": name})
    if not tests or len({(t["classname"], t["name"]) for t in tests}) != len(tests):
        raise PrerequisiteError("GTest inventory is empty or ambiguous")
    return tests


def normalize_junit(raw: Path, destination: Path, suite: Suite) -> None:
    if raw.stat().st_size > 16 * 1024 * 1024:
        raise PrerequisiteError("JUnit exceeds reporter size limit")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", raw.read_bytes(), re.IGNORECASE):
        raise PrerequisiteError("JUnit entities and DTDs are unsupported")
    tree = ET.parse(raw)
    if suite.kind == "gtest":
        # GTest lists disabled cases in XML despite excluding them from its
        # default selection. Retain raw XML, and project only these explicitly
        # unexecuted cases out of both the inventory and selected-result XML.
        excluded = [
            case
            for case in tree.iter("testcase")
            if (
                "DISABLED_" in case.get("classname", "")
                or case.get("name", "").startswith("DISABLED_")
            )
            and case.get("status") == "notrun"
        ]
        for element in tree.iter():
            removed = [case for case in element.iter("testcase") if case in excluded]
            if element.tag in {"testsuite", "testsuites"} and removed:
                for key, count in (
                    ("tests", len(removed)),
                    ("disabled", len(removed)),
                    (
                        "skipped",
                        sum(case.find("skipped") is not None for case in removed),
                    ),
                ):
                    if key in element.attrib:
                        element.set(key, str(int(element.get(key)) - count))
            for case in list(element):
                if case in excluded:
                    element.remove(case)
    for case in tree.iter("testcase"):
        if suite.kind != "gtest":
            case.set("classname", suite.id)
        if not case.get("name"):
            raise PrerequisiteError("JUnit testcase has no name")
    if not list(tree.iter("testcase")):
        raise PrerequisiteError("JUnit has no testcases")
    # Preserve every original aggregate counter and nesting relationship so
    # sanitization cannot turn contradictory XML into passing evidence.
    for element in tree.iter():
        if element.tag in {"testsuites", "testsuite"}:
            allowed = {
                "name",
                "tests",
                "failures",
                "errors",
                "skipped",
                "disabled",
                "time",
            }
        elif element.tag == "testcase":
            allowed = {"classname", "name", "time", "status", "result"}
        else:
            allowed = set()
        element.attrib = {
            key: value for key, value in element.attrib.items() if key in allowed
        }
        element.text = None
        element.tail = None
        for child in list(element):
            if child.tag in {"system-out", "system-err", "properties"}:
                element.remove(child)
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def probe_provider() -> int:
    # Run in an audited child so SONAME lookup uses the selected distribution.
    library = ctypes.CDLL("libhsa-runtime64.so.1")
    getattr(library, "hsa_init")
    return 0


def foundation(output: Path) -> int:
    """Measure HSA API contracts in an isolated, loader-audited process."""
    library = ctypes.CDLL("libhsa-runtime64.so.1")
    root = ET.Element("testsuite", name="hsa-foundation", tests="3")
    initialization = int(library.hsa_init())
    failures = 0
    case = ET.SubElement(root, "testcase", classname="hsa", name="init")
    if initialization:
        ET.SubElement(case, "failure", message=f"HSA status {initialization:#x}")
        failures += 1
    case = ET.SubElement(root, "testcase", classname="hsa", name="enumerate-gpu")
    if initialization:
        ET.SubElement(case, "skipped")
    else:
        devices = []
        callback_type = ctypes.CFUNCTYPE(
            ctypes.c_uint32, ctypes.c_uint64, ctypes.c_void_p
        )
        library.hsa_agent_get_info.argtypes = [
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_void_p,
        ]

        def agent_callback(agent: int, data: object) -> int:
            device = ctypes.c_uint32()
            status = library.hsa_agent_get_info(agent, 17, ctypes.byref(device))
            if not status:
                devices.append(device.value)
            return status

        library.hsa_iterate_agents.argtypes = [callback_type, ctypes.c_void_p]
        status = int(library.hsa_iterate_agents(callback_type(agent_callback), None))
        if status or 1 not in devices:
            ET.SubElement(
                case,
                "failure",
                message=f"HSA enumeration status {status:#x}; GPU agents {devices.count(1)}",
            )
            failures += 1
    case = ET.SubElement(root, "testcase", classname="hsa", name="shutdown")
    if initialization:
        ET.SubElement(case, "skipped")
    else:
        status = int(library.hsa_shut_down())
        if status:
            ET.SubElement(case, "failure", message=f"HSA status {status:#x}")
            failures += 1
    root.set("failures", str(failures))
    root.set("skipped", "2" if initialization else "0")
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return 1 if failures else 0


def build_triage(build: Path) -> dict[str, object]:
    """Use measured END records and fresh stamps, never diagnostic keywords."""
    graph_path = build / "therock_consumer_graph.json"
    graph = json.loads(graph_path.read_text()) if graph_path.is_file() else {}
    failures = []
    for path, end in find_failed_logs(build / "logs"):
        matches = [
            name for name in graph if path.name.lower().startswith(name.lower() + "_")
        ]
        phase = re.search(r"_(configure|build|install)\.log$", path.name)
        failures.append(
            {
                "subproject": max(matches, key=len) if matches else None,
                "phase": phase[1] if phase else None,
                "exit_code": int(end.split("\t")[-1]),
                "log": str(path.relative_to(build)),
            }
        )
    stamps = {}
    for initialization in build.rglob("*_init.cmake"):
        content = initialization.read_text()
        target_match = re.search(r'set\(THEROCK_SUBPROJECT_TARGET "([^"]+)"\)', content)
        stamp_match = re.search(r'set\(THEROCK_BUILD_STAMP_FILE "([^"]+)"\)', content)
        if target_match is None or stamp_match is None:
            continue
        target = target_match[1].lower()
        stamp_directory = Path(stamp_match[1]).parent
        if target not in graph or not stamp_directory.is_relative_to(build):
            raise PrerequisiteError(
                "Generated subproject stamp mapping differs from the active build graph"
            )
        for phase in ("configure", "build", "stage"):
            if (stamp_directory / f"{phase}.stamp").is_file():
                stamps.setdefault(target, []).append(phase)
    return {
        "schema_version": 1,
        "consumer_graph": graph,
        "failures": failures,
        "completed_phases": stamps,
    }


@dataclass
class Job:
    # Stable graph node identity.
    id: str
    # Public component name.
    component: str
    # Explicit dependency edges.
    needs: list[str]
    # Build until an actual test inventory has been measured.
    kind: str = "build"
    # Per-provider terminal observations; absent means incomplete.
    observations: dict[str, dict[str, object]] = field(default_factory=dict)
    # Measured test inventory, never placeholder passing coverage.
    expected_tests: list[dict[str, str]] = field(default_factory=list)
    # Immutable test source/configuration identity.
    test_identity: dict[str, str] = field(default_factory=dict)

    def manifest(self) -> dict[str, object]:
        result = {
            "id": self.id,
            "kind": self.kind,
            "component": self.component,
            "needs": self.needs,
            "observations": self.observations,
        }
        if self.kind == "test":
            result.update(
                expected_tests=self.expected_tests, test_identity=self.test_identity
            )
        return result


class Transition:
    def __init__(self, args: argparse.Namespace, plan: Plan) -> None:
        self.args = args
        self.plan = plan
        self.work = args.work_dir.resolve()
        self.runner = Runner(self.work / "diagnostics")
        self.environment = {
            "os": "unavailable",
            "kernel": "unavailable",
            "driver": "unavailable",
            "gpu": args.gpu,
            "container": args.container_image.rsplit("@", 1)[-1],
        }
        self.jobs = {
            name: Job(name, "runtime", needs)
            for name, needs in (
                ("prerequisites", []),
                ("sources", ["prerequisites"]),
                ("configure", ["sources"]),
                ("dist", ["configure"]),
                ("artifacts", ["dist"]),
                ("provider", ["dist"]),
            )
        }
        for suite in plan.suites:
            self.jobs[f"inventory-{suite.id}"] = Job(
                f"inventory-{suite.id}", suite.component, ["provider", "artifacts"]
            )
            if suite.component != "rocm-runtimes" and suite.id != "build-integrity":
                self.jobs[f"inventory-{suite.id}"].needs.append("test-hsa-foundation")
        self.jobs["test-hsa-foundation"] = Job(
            "test-hsa-foundation",
            "hsa",
            ["provider"],
            "test",
            expected_tests=FOUNDATION_TESTS,
            test_identity={
                "suite": "hsa-foundation",
                "revision": args.therock_ref,
                "configuration": "initialize-enumerate-shutdown-v1",
            },
        )
        self.profiles = {
            role: {
                "provider": provider,
                "sources": {"therock": args.therock_ref, "runtime": args.runtime_ref},
                "toolchain": {
                    "id": "unavailable",
                    "sha256": hashlib.sha256(b"unavailable").hexdigest(),
                },
                "configuration": {
                    "build_type": "Release",
                    "runtime_flag": "ON" if role == "candidate" else "OFF",
                    "hip_flag": "ON",
                    "plan": plan.sha256,
                    "gpu_target": args.gpu,
                    "gpu_family": args.gpu_family,
                    "cts_qualification": "not-requested",
                    "container_contract": "linux-kfd-dri-seccomp-unconfined-memlock-unlimited-nofile1048576",
                },
            }
            for role, provider in PROVIDERS.items()
        }
        self.active: tuple[Job, str] | None = None
        self.evidence = args.evidence_url
        self.run_id = os.environ.get("GITHUB_RUN_ID", "local-" + uuid.uuid4().hex)
        self.image_labels: dict[str, str] = {}

    def checkpoint(self) -> None:
        write_json(
            self.work / "manifest.json",
            {
                "schema_version": 1,
                "repository": self.args.therock_repository,
                "run": {
                    "id": self.run_id,
                    "attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
                    "event": os.environ.get("GITHUB_EVENT_NAME", "local"),
                    "ref": os.environ.get("GITHUB_REF", "refs/heads/local"),
                    "sha": os.environ.get("GITHUB_SHA", self.args.therock_ref),
                    "url": (
                        f"https://github.com/{self.args.therock_repository}/actions/runs/{self.run_id}"
                        if "GITHUB_RUN_ID" in os.environ
                        else None
                    ),
                },
                "profiles": self.profiles,
                "jobs": [job.manifest() for job in self.jobs.values()],
            },
        )

    def observe(self, job: Job, role: str, result: CommandResult) -> None:
        job.observations[role] = {
            **asdict(result),
            "environment": self.environment.copy(),
            "evidence_url": self.evidence,
            "reproduction_url": (
                f"https://github.com/{self.args.therock_repository}/blob/{self.args.therock_ref}/docs/development/runtime_transition.md"
                if "GITHUB_RUN_ID" in os.environ
                else None
            ),
        }
        if "GITHUB_RUN_ID" not in os.environ:
            job.observations[role].update(
                evidence_locator="diagnostics.tar.gz",
                reproduction_locator="diagnostics/plan.json",
            )
        self.checkpoint()

    def command(
        self,
        job: str,
        role: str,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int = 86400,
        output: Path | None = None,
        infrastructure: bool = False,
    ) -> bool:
        node = self.jobs[job]
        self.active = (node, role)
        # Checkpoint the running state as incomplete, including hard termination.
        self.observe(node, role, CommandResult("infrastructure_failure", False))
        result = self.runner.run(
            command, cwd=cwd, env=env, timeout=timeout, output=output
        )
        if infrastructure and result.status != "success":
            result.status = "infrastructure_failure"
        self.observe(node, role, result)
        self.active = None
        return result.status == "success"

    def prepare_source(self, role: str, environment: dict[str, str]) -> Path | None:
        source = self.work / role / "source"
        source.parent.mkdir(parents=True, exist_ok=True)
        source_git = getattr(self.args, "source_git_dir", None)
        if source_git is not None:
            if not self.command(
                "sources",
                role,
                ["git", "init", str(source)],
                cwd=self.work,
                env=environment,
                infrastructure=True,
            ):
                return None
            (source / ".git/objects/info/alternates").write_text(
                str(source_git / "objects") + "\n"
            )
            if (source_git / "shallow").is_file():
                shutil.copyfile(source_git / "shallow", source / ".git/shallow")
            capture(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    f"https://github.com/{self.args.therock_repository}.git",
                ],
                cwd=source,
                env=environment,
            )
        else:
            origin = (
                str(self.args.therock_checkout.resolve())
                if self.args.therock_checkout
                else f"https://github.com/{self.args.therock_repository}.git"
            )
            if not self.command(
                "sources",
                role,
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    origin,
                    str(source),
                ],
                cwd=self.work,
                env=environment,
                infrastructure=True,
            ):
                return None
        for command in (
            *(
                []
                if source_git is not None
                else [["git", "fetch", "origin", self.args.therock_ref]]
            ),
            ["git", "checkout", "--detach", self.args.therock_ref],
            [
                sys.executable,
                str(source / "build_tools/fetch_sources.py"),
                "--depth",
                "1",
                "--jobs",
                str(self.args.jobs),
                "--external-repo-config",
                json.dumps(
                    {
                        "repository": "ROCm/rocm-systems",
                        "ref": self.args.runtime_ref,
                        "submodule_path": "rocm-systems",
                    }
                ),
            ],
        ):
            if not self.command(
                "sources",
                role,
                command,
                cwd=source,
                env=environment,
                infrastructure=True,
            ):
                return None
        identities = source_identity(source, environment)
        if (
            identities["therock"] != self.args.therock_ref
            or identities["runtime"] != self.args.runtime_ref
        ):
            raise PrerequisiteError("Fetched sources differ from requested pins")
        self.profiles[role]["sources"] = identities
        if self.image_labels:
            runtime = source / "rocm-systems/runtimes"
            lock_hash = digest(runtime / "Cargo.lock")
            toolchain = tomllib.loads((runtime / "rust-toolchain.toml").read_text())[
                "toolchain"
            ]["channel"]
            if (
                lock_hash
                != self.image_labels["org.therock.runtime-transition.cargo-lock-sha256"]
                or toolchain
                != self.image_labels["org.therock.runtime-transition.rust-toolchain"]
            ):
                raise PrerequisiteError(
                    "Runtime Cargo.lock or Rust toolchain changed; rebuild the pinned image"
                )
        write_json(self.work / "diagnostics" / f"{role}-sources.json", identities)
        self.checkpoint()
        return source

    def provider(
        self, role: str, source: Path, build: Path, environment: dict[str, str]
    ) -> dict[str, str]:
        root = build / "dist/rocm"
        canonical = (root / "lib/libhsa-runtime64.so.1").resolve(strict=True)
        stage = build / "core" / STAGES[role] / "stage/lib/libhsa-runtime64.so.1"
        if not canonical.is_relative_to(root.resolve()) or digest(canonical) != digest(
            stage
        ):
            raise PrerequisiteError(
                "Installed canonical HSA does not match the selected provider stage"
            )
        if role == "candidate" and (build / "core/ROCR-Runtime/stage").exists():
            raise PrerequisiteError("Candidate contains a ROCR build")
        compiler = root / "lib/llvm/bin/clang"
        write_json(
            self.work / "diagnostics" / f"{role}-compiler.json",
            {
                "llvm_source": self.profiles[role]["sources"]["compiler/amd-llvm"],
                "sha256": digest(compiler),
            },
        )
        audit = self.work / "diagnostics/loader-audit.so"
        if not audit.exists():
            if not self.command(
                "provider",
                role,
                [
                    "cc",
                    "-shared",
                    "-fPIC",
                    "-O2",
                    str(SUPPORT_DIR / "loader_audit.c"),
                    "-o",
                    str(audit),
                ],
                cwd=source,
                env=environment,
            ):
                raise PrerequisiteError("Cannot build loader audit module")
        selected = clean_environment(root)
        selected.update(
            AMDGPU_FAMILIES=self.args.gpu_family,
            LD_AUDIT=str(audit),
            RUNTIME_TRANSITION_HSA=str(canonical),
            RUNTIME_TRANSITION_AUDIT_LOG=str(
                self.work / "diagnostics" / f"{role}-loader.log"
            ),
        )
        if not self.command(
            "provider",
            role,
            [sys.executable, str(Path(__file__).resolve()), "probe-provider"],
            cwd=source,
            env=selected,
            timeout=300,
            infrastructure=True,
        ):
            raise PrerequisiteError("Natural HSA loader selection failed")
        if not Path(selected["RUNTIME_TRANSITION_AUDIT_LOG"]).is_file():
            raise PrerequisiteError("Provider probe produced no loader evidence")
        write_json(
            self.work / "diagnostics" / f"{role}-provider.json",
            {
                "canonical": str(canonical),
                "stage": str(stage),
                "sha256": digest(canonical),
            },
        )
        return selected

    def test_suite(
        self,
        role: str,
        source: Path,
        build: Path,
        environment: dict[str, str],
        suite: Suite,
    ) -> None:
        directory = self.work / "results" / role / suite.id
        directory.mkdir(parents=True)
        node = f"inventory-{suite.id}"
        env = environment.copy()
        if suite.component == "rocm-runtimes":
            private = build / "core/rocm-runtimes/build/upstream/lib"
            selected_image = (private / "libhsa-runtime64.so.1").resolve(strict=True)
            staged_image = (
                build
                / "core/rocm-runtimes/stage/lib/rocm-runtimes/lib/libhsa-runtime64.so.1"
            )
            if digest(selected_image) != digest(staged_image):
                raise PrerequisiteError(
                    "Runtime selftest image differs from the private producer"
                )
            env["LD_LIBRARY_PATH"] = f"{private}:{env['LD_LIBRARY_PATH']}"
            env["RUNTIME_TRANSITION_HSA"] = str(selected_image)
            env["RUNTIME_TRANSITION_AUDIT_LOG"] = str(
                self.work / "diagnostics" / f"{role}-{suite.id}-loader.log"
            )
        path = build / suite.path
        if suite.kind == "component":
            real_ctest = shutil.which("ctest", path=SYSTEM_PATH)
            if real_ctest is None:
                raise PrerequisiteError("CTest is unavailable")
            shim = directory / "shim"
            shim.mkdir()
            executable = shim / "ctest"
            executable.write_text(
                f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(SUPPORT_DIR / 'ctest_driver.py')!r}, run_name='__main__')\n"
            )
            executable.chmod(0o755)
            env.update(
                PATH=f"{shim}:{env['PATH']}",
                RUNTIME_TRANSITION_CTEST=real_ctest,
                RUNTIME_TRANSITION_SUITE_OUTPUT=str(directory),
                TEST_COMPONENT=suite.component,
                TEST_TYPE=suite.category,
            )
            command = [
                sys.executable,
                str(
                    source
                    / "build_tools/github_actions/test_executable_scripts"
                    / suite.script
                ),
            ]
            # The interposer checkpoints the exact post-filter inventory before
            # invoking the real CTest, even when the component later times out.
            self.active = (self.jobs[node], role)
            self.observe(
                self.jobs[node], role, CommandResult("infrastructure_failure", False)
            )
            result = self.runner.run(
                command, cwd=source, env=env, timeout=suite.timeout
            )
            inventories = sorted(directory.glob("*.inventory.json"))
            tests = [
                test
                for inventory in inventories
                for test in ctest_inventory(inventory, suite.id)
            ]
            raw_junit = sorted(directory.glob("*.xml"))
        else:
            inventory = directory / "selection.inventory.json"
            if suite.kind == "ctest":
                command = ["ctest", "--test-dir", str(path)]
                if suite.label:
                    command += ["--label-regex", suite.label]
                if suite.exclude_label:
                    command += ["--label-exclude", suite.exclude_label]
                discovery = [*command, "--show-only=json-v1"]
            else:
                command = [str(path)]
                discovery = [*command, "--gtest_list_tests"]
            if not self.command(
                node,
                role,
                discovery,
                cwd=source,
                env=env,
                timeout=300,
                output=inventory,
                infrastructure=True,
            ):
                return
            tests = (
                ctest_inventory(inventory, suite.id)
                if suite.kind == "ctest"
                else gtest_inventory(inventory)
            )
            raw = directory / "raw.xml"
            if suite.kind == "ctest":
                command += [
                    "--no-tests=error",
                    "--output-on-failure",
                    "--timeout",
                    str(suite.timeout),
                    "--output-junit",
                    str(raw),
                ]
            else:
                command += [f"--gtest_output=xml:{raw}"]
            result = None
            raw_junit = [raw]
        if not tests or len(tests) > 20000:
            raise PrerequisiteError(
                "Selected test inventory is empty or exceeds reporter bounds"
            )
        name = f"test-{suite.id}"
        if name not in self.jobs:
            self.jobs[name] = Job(
                name,
                suite.component,
                [node],
                "test",
                expected_tests=tests,
                test_identity={
                    "suite": suite.id,
                    "revision": self.args.therock_ref,
                    "configuration": f"{suite.category}-{self.plan.sha256}",
                },
            )
            if suite.component == "rocm-runtimes":
                self.jobs[name].test_identity.update(
                    subject="runtime-selftests",
                    implementation_revision=self.args.runtime_ref,
                    dependency_boundary="private-producer",
                )
        elif self.jobs[name].expected_tests != tests:
            raise PrerequisiteError("Paired test inventories differ")
        self.observe(self.jobs[node], role, CommandResult("success", True))
        job = self.jobs[name]
        self.active = (job, role)
        self.observe(job, role, CommandResult("infrastructure_failure", False))
        if result is None:
            result = self.runner.run(
                command, cwd=source, env=env, timeout=suite.timeout
            )
        self.observe(job, role, result)
        junit = []
        for index, raw in enumerate(raw_junit):
            if not raw.is_file():
                continue
            normalized = directory / f"report-{index}.junit"
            normalize_junit(raw, normalized, suite)
            junit.append(
                {
                    "path": str(normalized.relative_to(self.work)),
                    "sha256": digest(normalized),
                }
            )
        job.observations[role]["junit"] = junit
        self.checkpoint()
        self.active = None

    def run_profile(self, role: str) -> None:
        environment = clean_environment()
        environment.update(
            CMAKE_BUILD_PARALLEL_LEVEL=str(self.args.jobs),
            CARGO_BUILD_JOBS=str(self.args.jobs),
        )
        cargo_seed = Path("/opt/runtime-transition/cargo")
        if cargo_seed.is_dir():
            cargo_home = self.work / role / "cargo-home"
            shutil.copytree(cargo_seed, cargo_home)
            environment["CARGO_HOME"] = str(cargo_home)
        source = self.prepare_source(role, environment)
        if source is None:
            return
        build = self.work / role / "build"
        command = [
            "cmake",
            "-S",
            str(source),
            "-B",
            str(build),
            "-GNinja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DTHEROCK_AMDGPU_FAMILIES={self.args.gpu_family}",
            f"-DTHEROCK_DIST_AMDGPU_FAMILIES={self.args.gpu_family}",
            f"-DTHEROCK_TEST_AMDGPU_FAMILIES={self.args.gpu_family}",
            "-DTHEROCK_ENABLE_ALL=ON",
            "-DTHEROCK_BUILD_TESTING=ON",
            "-DBUILD_TESTING=ON",
            "-DTHEROCK_ENABLE_ROCM_RUNTIMES=ON",
            f"-DROCM_RUNTIMES_CARGO_JOBS={self.args.jobs}",
            f"-DTHEROCK_FLAG_REPLACE_ROCR_WITH_RUST_HSA={'ON' if role == 'candidate' else 'OFF'}",
        ]
        if "CARGO_HOME" in environment:
            command += [f"-DROCM_RUNTIMES_CARGO_HOME={environment['CARGO_HOME']}"]
        command += [
            f"-D{key}={value}" for key, value in sorted(self.plan.cmake.items())
        ]
        if not self.command("configure", role, command, cwd=source, env=environment):
            return
        cache = (build / "CMakeCache.txt").read_text()
        for key, expected in {
            "THEROCK_ENABLE_ROCM_RUNTIMES": "ON",
            "THEROCK_FLAG_REPLACE_ROCR_WITH_RUST_HSA": (
                "ON" if role == "candidate" else "OFF"
            ),
            **self.plan.cmake,
        }.items():
            if not re.search(rf"^{key}:BOOL={expected}$", cache, re.MULTILINE):
                raise PrerequisiteError(
                    "Runtime option was not consumed by this source revision"
                )
        built = self.command(
            "dist",
            role,
            [
                "cmake",
                "--build",
                str(build),
                "--parallel",
                str(self.args.jobs),
                "--target",
                "dist",
                "--",
                "-k",
                "0",
            ],
            cwd=source,
            env=environment,
        )
        triage = build_triage(build)
        write_json(self.work / "diagnostics" / f"{role}-build-triage.json", triage)
        self.record_build_triage(role, triage)
        if not built:
            return
        if not self.command(
            "artifacts",
            role,
            ["cmake", "--build", str(build), "--target", "artifacts"],
            cwd=source,
            env=environment,
        ):
            return
        if not any((build / "artifacts").iterdir()):
            raise PrerequisiteError("Artifact target produced no artifacts")
        selected = self.provider(role, source, build, environment)
        runtime_metadata = json.loads(
            (
                build / "dist/rocm/lib/rocm-runtimes/share/rocm-runtimes/runtime.json"
            ).read_text()
        )
        write_json(
            self.work / "diagnostics" / f"{role}-runtime-metadata.json",
            runtime_metadata,
        )
        foundation_passed = self.measure_foundation(role, source, selected)
        for suite in self.plan.suites:
            if (
                not foundation_passed
                and suite.component != "rocm-runtimes"
                and suite.id != "build-integrity"
            ):
                continue
            try:
                self.test_suite(role, source, build, selected, suite)
            except (OSError, ValueError, ET.ParseError) as error:
                self.failure(f"inventory-{suite.id}", role, error)

    def measure_foundation(
        self, role: str, source: Path, environment: dict[str, str]
    ) -> bool:
        directory = self.work / "results" / role / "hsa-foundation"
        directory.mkdir(parents=True)
        raw = directory / "raw.xml"
        passed = self.command(
            "test-hsa-foundation",
            role,
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "foundation",
                "--output",
                str(raw),
            ],
            cwd=source,
            env=environment,
            timeout=300,
        )
        if raw.is_file():
            output = directory / "report-0.junit"
            normalize_junit(raw, output, Suite("hsa-foundation", "hsa", "gtest"))
            self.jobs["test-hsa-foundation"].observations[role]["junit"] = [
                {"path": str(output.relative_to(self.work)), "sha256": digest(output)}
            ]
            self.checkpoint()
        return passed

    def record_build_triage(self, role: str, triage: dict[str, object]) -> None:
        graph = triage["consumer_graph"]
        if not graph:
            return

        def normalized(target: str) -> str:
            return (
                "hsa-provider"
                if target in {"rocr-runtime", "rust-hsa", "hsakmt"}
                else re.sub(r"[^a-z0-9-]", "-", target.lower())
            )

        members = {}
        for target in graph:
            members.setdefault(normalized(target), []).append(target)
        for component, targets in members.items():
            dependencies = {
                normalized(dependency)
                for dependency, data in graph.items()
                if any(target in data["consumers"] for target in targets)
            } - {component}
            for phase, previous in (
                ("configure", None),
                ("build", "configure"),
                ("install", "build"),
            ):
                name = f"build-{component}-{phase}"
                needs = (
                    [f"build-{component}-{previous}"]
                    if previous
                    else [
                        "configure",
                        *[f"build-{dependency}-install" for dependency in dependencies],
                    ]
                )
                if name not in self.jobs:
                    self.jobs[name] = Job(name, component, needs)
                job = self.jobs[name]
                job.needs = sorted(set(job.needs + needs))
                failed = any(
                    item["subproject"] in targets and item["phase"] == phase
                    for item in triage["failures"]
                )
                completed = all(
                    ("stage" if phase == "install" else phase)
                    in triage["completed_phases"].get(target, [])
                    for target in targets
                )
                if failed or completed:
                    self.observe(
                        job,
                        role,
                        CommandResult("failure" if failed else "success", True),
                    )
        self.jobs["dist"].needs = sorted(
            {
                *self.jobs["dist"].needs,
                *[f"build-{normalized(target)}-install" for target in graph],
            }
        )
        self.checkpoint()

    def failure(self, job: str, role: str, error: BaseException) -> None:
        write_json(
            self.work / "diagnostics" / f"{role}-{job}-error.json",
            {"error": str(error)},
        )
        self.observe(
            self.jobs[job], role, CommandResult("infrastructure_failure", False)
        )
        self.active = None

    def run(self) -> int:
        self.work.mkdir(parents=True, exist_ok=False)
        (self.work / "diagnostics").mkdir()
        (self.work / "home").mkdir()
        write_json(
            self.work / "diagnostics/plan.json",
            {
                "cmake": self.plan.cmake,
                "suites": [asdict(suite) for suite in self.plan.suites],
                "sha256": self.plan.sha256,
            },
        )
        self.checkpoint()
        try:
            if self.args.image_metadata is None:
                raise PrerequisiteError(
                    "Run requires inspected image metadata from the container launcher"
                )
            if self.args.therock_checkout:
                validate_checkout(self.args.therock_checkout, self.args.therock_ref)
            if self.args.image_metadata:
                metadata = json.loads(self.args.image_metadata.read_text())
                if metadata["Id"] != self.args.container_image:
                    raise PrerequisiteError(
                        "Inspected and running image identities differ"
                    )
                labels = metadata["Labels"]
                provisioning = validate_image_provenance(
                    metadata,
                    Path("/usr/local/share/runtime-transition/provenance.json"),
                )
                write_json(self.work / "diagnostics/provisioning.json", provisioning)
                for role in PROVIDERS:
                    self.profiles[role]["toolchain"] = {
                        "id": "container-build-toolchain",
                        "sha256": metadata["Id"].removeprefix("sha256:"),
                    }
                    self.profiles[role]["configuration"]["provisioning"] = (
                        hashlib.sha256(
                            json.dumps(provisioning, sort_keys=True).encode()
                        ).hexdigest()
                    )

                self.image_labels = labels
                for role in PROVIDERS:
                    for suffix in (
                        "runtime-ref",
                        "cargo-lock-sha256",
                        "rust-toolchain",
                    ):
                        self.profiles[role]["configuration"][suffix] = labels[
                            f"org.therock.runtime-transition.{suffix}"
                        ]
                write_json(self.work / "diagnostics/image.json", metadata)
            self.environment = hardware_identity(
                self.args.gpu, self.args.container_image
            )
            for role in PROVIDERS:
                self.observe(
                    self.jobs["prerequisites"], role, CommandResult("success", True)
                )
                try:
                    self.run_profile(role)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.failure(
                        self.active[0].id if self.active else "provider", role, error
                    )
        except KeyboardInterrupt:
            if self.active:
                self.observe(*self.active, CommandResult("cancelled", True))
            return 130
        except (OSError, ValueError) as error:
            for role in PROVIDERS:
                self.failure("prerequisites", role, error)
        finally:
            self.checkpoint()
            archive_diagnostics(self.work)
        return (
            0
            if all(
                len(job.observations) == 2
                and all(obs["status"] == "success" for obs in job.observations.values())
                for job in self.jobs.values()
            )
            else 1
        )


def initialize_missing(transition: Transition) -> None:
    """Also used after a killed runner or an early workflow setup failure."""
    if (transition.work / "manifest.json").exists():
        return
    transition.work.mkdir(parents=True, exist_ok=True)
    (transition.work / "diagnostics").mkdir(exist_ok=True)
    transition.checkpoint()


def validate_checkout(checkout: Path, expected: str) -> None:
    environment = {**clean_environment(), "GIT_OPTIONAL_LOCKS": "0"}
    if checkout.is_file():
        # The launcher verified a clean source before exporting this immutable
        # commit. A bundle exposes no mutable checkout or external .git path.
        refs = capture(
            ["git", "bundle", "list-heads", str(checkout)],
            cwd=checkout.parent,
            env=environment,
        )
        if refs.split()[0] != expected:
            raise PrerequisiteError("Source bundle does not contain the requested HEAD")
        return
    if capture(["git", "rev-parse", "HEAD"], cwd=checkout, env=environment) != expected:
        raise PrerequisiteError("Local source HEAD differs from requested commit")
    if capture(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        cwd=checkout,
        env=environment,
    ):
        raise PrerequisiteError(
            "Local source must have no tracked or untracked changes"
        )


def git_object_stores(common: Path) -> list[Path]:
    """Resolve trusted local alternates while preserving their relative paths."""
    stores = []
    pending = [(common / "objects").resolve()]
    while pending:
        store = pending.pop(0)
        if store in stores:
            continue
        if not store.is_dir() or len(stores) >= 64:
            raise PrerequisiteError(
                "Local Git object stores are missing or exceed the bounded alternate graph"
            )
        stores.append(store)
        alternates = store / "info/alternates"
        if alternates.is_file():
            for entry in alternates.read_text().splitlines():
                path = Path(entry)
                pending.append((path if path.is_absolute() else store / path).resolve())
    return stores


def create_orchestration_snapshot(args: argparse.Namespace) -> Path:
    source = args.therock_checkout or SCRIPT_DIR.parent
    if args.therock_checkout:
        validate_checkout(source, args.therock_ref)
    try:
        return snapshot.create_orchestration_snapshot(
            args.work_dir.resolve(), source, args.therock_ref, clean_environment()
        )
    except snapshot.SnapshotError as error:
        raise PrerequisiteError(str(error)) from error


def verify_orchestration_snapshot(work: Path, expected: str) -> Path:
    try:
        return snapshot.verify_orchestration_snapshot(work, expected)
    except snapshot.SnapshotError as error:
        raise PrerequisiteError(str(error)) from error


def archive_diagnostics(work: Path) -> None:
    """Preserve complete logs/caches privately without copying full build trees."""
    with tarfile.open(work / "diagnostics.tar.gz", "w:gz") as archive:
        for directory in (work / "diagnostics", work / "results"):
            if directory.exists():
                archive.add(directory, arcname=directory.name)
        for role in PROVIDERS:
            build = work / role / "build"
            for path in (
                build / "logs",
                *build.rglob("CMakeCache.txt"),
                *build.rglob("LastTest.log"),
            ):
                if path.exists():
                    archive.add(path, arcname=str(path.relative_to(work)))


def finish(transition: Transition) -> int:
    root = verify_orchestration_snapshot(transition.work, transition.args.therock_ref)
    initialize_missing(transition)
    reporter = root / "build_tools/runtime_transition_report.py"
    return subprocess.call(
        [
            sys.executable,
            str(reporter),
            "--input",
            str(transition.work / "manifest.json"),
            "--output-dir",
            str(transition.work / "reports"),
        ],
        env=clean_environment(),
    )


def launch_container(args: argparse.Namespace, plan: Plan) -> int:
    work = args.work_dir.resolve()
    if work.exists():
        raise ValueError(
            "Use a new work directory; builds and results are never reused"
        )
    work.parent.mkdir(parents=True, exist_ok=True)
    snapshot = create_orchestration_snapshot(args)
    if IMAGE.fullmatch(args.container_image):
        subprocess.run(["docker", "pull", args.container_image], check=True)
    metadata = json.loads(
        subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                args.container_image,
            ],
            text=True,
        )
    )
    image_id = metadata["Id"]
    if not LOCAL_IMAGE.fullmatch(image_id):
        raise ValueError("Docker returned an invalid immutable image ID")
    if LOCAL_IMAGE.fullmatch(args.container_image):
        if image_id != args.container_image:
            raise ValueError("Local image ID did not match inspection")
    elif args.container_image not in metadata.get("RepoDigests", []):
        raise ValueError("Requested registry digest did not match inspection")
    metadata_path = work.parent / f".{work.name}-image.json"
    write_json(
        metadata_path,
        {
            "Id": image_id,
            "RepoDigests": metadata.get("RepoDigests", []),
            "Labels": metadata.get("Config", {}).get("Labels", {}),
            "RuntimeOptions": GPU_CONTAINER_OPTIONS,
        },
    )
    source_git = None
    object_stores = []
    if args.therock_checkout:
        validate_checkout(args.therock_checkout, args.therock_ref)
        source_git = Path(
            capture(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=args.therock_checkout,
                env=clean_environment(),
            )
        )
        object_stores = git_object_stores(source_git)
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--device",
        "/dev/kfd",
        "--device",
        "/dev/dri",
        *GPU_CONTAINER_OPTIONS,
    ]
    for group in sorted(
        {
            path.stat().st_gid
            for path in [Path("/dev/kfd"), *Path("/dev/dri").glob("*")]
            if path.exists()
        }
    ):
        command += ["--group-add", str(group)]
    command += [
        "--mount",
        f"type=bind,src={snapshot},dst=/orchestrator,readonly",
        "--mount",
        f"type=bind,src={work.parent},dst=/runs",
        "--mount",
        f"type=bind,src={snapshot},dst=/runs/{snapshot.name},readonly",
        "--mount",
        f"type=bind,src={args.plan.resolve()},dst=/plan.json,readonly",
        "--mount",
        f"type=bind,src={metadata_path},dst=/image-metadata.json,readonly",
        "--env",
        f"HOME=/runs/{work.name}/home",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        image_id,
        "python3",
        "/orchestrator/build_tools/runtime_transition.py",
        "run",
        "--work-dir",
        f"/runs/{work.name}",
        "--plan",
        "/plan.json",
        "--therock-repository",
        args.therock_repository,
        "--therock-ref",
        args.therock_ref,
        "--runtime-ref",
        args.runtime_ref,
        "--container-image",
        image_id,
        "--image-metadata",
        "/image-metadata.json",
        "--gpu",
        args.gpu,
        "--gpu-family",
        args.gpu_family,
        "--jobs",
        str(args.jobs),
    ]
    if args.evidence_url is not None:
        command += ["--evidence-url", args.evidence_url]
    for key in (
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "GITHUB_SHA",
    ):
        if key in os.environ:
            command[2:2] = ["--env", f"{key}={os.environ[key]}"]
    if source_git is not None:
        command[2:2] = [
            "--mount",
            f"type=bind,src={source_git},dst={source_git},readonly",
        ]
        for store in object_stores:
            command[2:2] = ["--mount", f"type=bind,src={store},dst={store},readonly"]
        command += ["--source-git-dir", str(source_git)]
    try:
        verify_orchestration_snapshot(work, args.therock_ref)
        result = subprocess.call(command)
    finally:
        # Docker startup failures and SIGKILL cannot flush the child's manifest.
        initialize_missing(Transition(args, plan))
    return result


def workflow_configuration() -> int:
    """Run on a hosted runner before evaluating self-hosted/container inputs."""
    image = os.environ.get("TRANSITION_IMAGE", "")
    gpu = os.environ.get("TRANSITION_GPU", "")
    family = os.environ.get("TRANSITION_GPU_FAMILY", "")
    labels = json.loads(os.environ.get("TRANSITION_RUNNER_LABELS", "[]"))
    if not IMAGE.fullmatch(image):
        raise ValueError(
            "RUNTIME_TRANSITION_IMAGE must be a no-ROCm image pinned by digest"
        )
    if not re.fullmatch(r"gfx[0-9a-f]+", gpu):
        raise ValueError("Set RUNTIME_TRANSITION_GPU to the exposed GPU target")
    if not re.fullmatch(r"gfx[A-Za-z0-9-]+", family):
        raise ValueError(
            "Set RUNTIME_TRANSITION_GPU_FAMILY to a valid distribution family"
        )
    if (
        not isinstance(labels, list)
        or not labels
        or not all(
            isinstance(label, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", label)
            for label in labels
        )
    ):
        raise ValueError(
            "Set RUNTIME_TRANSITION_RUNNER_LABELS to an explicit JSON label array"
        )
    if not {"self-hosted", "linux", "x64"}.issubset(labels) or len(labels) < 4:
        raise ValueError(
            "Runner labels require self-hosted, linux, x64 and a dedicated pool label"
        )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(
            f"image={image}\ngpu={gpu}\nfamily={family}\nlabels={json.dumps(labels)}\n"
        )
    return 0


def github_request(path: str, payload: dict[str, object] | None = None) -> object:
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        content = response.read()
    return json.loads(content) if content else None


def dispatch_worker() -> int:
    repository = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["TRANSITION_WORKER_REF"]
    expected = os.environ["TRANSITION_WORKER_SHA"]
    runtime_ref = os.environ["TRANSITION_RUNTIME_REF"]
    if (
        not REPOSITORY.fullmatch(repository)
        or not SHA.fullmatch(expected)
        or not SHA.fullmatch(runtime_ref)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch)
    ):
        raise ValueError(
            "Dispatcher requires an explicit worker branch and exact approved SHA"
        )
    metadata = github_request(
        f"/repos/{repository}/git/ref/heads/{urllib.parse.quote(branch, safe='/')}"
    )
    if metadata["object"]["sha"] != expected:
        raise ValueError(
            "Worker branch moved; approve its new commit before dispatching"
        )
    github_request(
        f"/repos/{repository}/actions/workflows/runtime_transition.yml/dispatches",
        {
            "ref": branch,
            "inputs": {"plan": "full", "runtime_ref": runtime_ref, "publish": "false"},
        },
    )
    print("Worker dispatch accepted; inspect its separate run for completion.")
    return 0


def job_evidence() -> int:
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    metadata = github_request(
        f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
    )
    matches = [job["html_url"] for job in metadata["jobs"] if job["name"] == "compare"]
    if len(matches) != 1:
        raise ValueError("Cannot identify this run's comparison job")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"url={matches[0]}\n")
    return 0


def bind_evidence(work: Path, url: str) -> int:
    manifest = json.loads((work / "manifest.json").read_text())
    if not re.fullmatch(
        re.escape(manifest["run"]["url"]) + r"/artifacts/[1-9][0-9]*", url
    ):
        raise ValueError("Evidence must be an artifact belonging to this run")
    for job in manifest["jobs"]:
        for observation in job["observations"].values():
            observation["evidence_url"] = url
    write_json(work / "manifest.json", manifest)
    return 0


def prepare_publication(work: Path, url: str) -> int:
    manifest = json.loads((work / "manifest.json").read_text())
    expected = manifest["profiles"]["baseline"]["sources"]["therock"]
    root = verify_orchestration_snapshot(work, expected)
    bind_evidence(work, url)
    result = subprocess.call(
        [
            sys.executable,
            str(root / "build_tools/runtime_transition_report.py"),
            "--input",
            str(work / "manifest.json"),
            "--output-dir",
            str(work / "reports"),
        ],
        env=clean_environment(),
    )
    if result not in {0, 1}:
        return result
    report_hash = digest(work / "reports/report.json")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"sha256={report_hash}\n")
    return 0


def publish_report(report_path: Path, expected: str, issue_repository: str) -> int:
    # Only this same workflow's trusted producer can supply the expected digest.
    # Native inventories and XML stay private on the build runner.
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(report_path) != expected:
        raise ValueError("Trusted producer report digest is missing or mismatched")
    report = json.loads(report_path.read_text())
    if report.get("schema_version") != 1 or report["run"]["event"] == "local":
        raise ValueError("Only a same-run Actions report can be published")
    reporter = importlib.import_module("runtime_transition_report")
    manifest = reporter.Manifest(
        repository=report["repository"],
        run=report["run"],
        profiles=report["profiles"],
        jobs=[],
    )
    reporter.publish_issues(
        manifest,
        report,
        issue_repository=issue_repository,
        environment=os.environ,
        api=reporter.GitHubAPI(github_token=os.environ["GITHUB_TOKEN"]),
    )
    return 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe-provider", help=argparse.SUPPRESS)
    foundation_command = commands.add_parser(
        "foundation", help="Measure HSA initialization, GPU enumeration, and shutdown"
    )
    foundation_command.add_argument("--output", type=Path, required=True)
    commands.add_parser(
        "workflow-configuration",
        help="Validate repository runner and image configuration",
    )
    commands.add_parser(
        "dispatch",
        help="Dispatch the explicitly approved worker branch from the default branch",
    )
    commands.add_parser(
        "job-evidence", help="Resolve the actual comparison job's public evidence URL"
    )
    bind = commands.add_parser(
        "bind-evidence", help="Bind immutable uploaded evidence to a completed manifest"
    )
    bind.add_argument("--work-dir", type=Path, required=True)
    bind.add_argument("--evidence-url", required=True)
    prepare = commands.add_parser(
        "prepare-publication",
        help="Bind public evidence and seal only the redacted report",
    )
    prepare.add_argument("--work-dir", type=Path, required=True)
    prepare.add_argument("--evidence-url", required=True)
    publish = commands.add_parser(
        "publish-report", help="Publish this workflow's exact trusted redacted report"
    )
    publish.add_argument("--report", type=Path, required=True)
    publish.add_argument("--expected-sha256", required=True)
    publish.add_argument("--issue-repository", required=True)
    for name in ("run", "container", "finish"):
        command = commands.add_parser(
            name,
            help={
                "run": "Run inside the pinned no-ROCm container",
                "container": "Launch a local paired run without mounting host ROCm",
                "finish": "Ensure a failure manifest exists and run the offline reporter",
            }[name],
        )
        command.add_argument(
            "--work-dir",
            type=Path,
            required=True,
            help="New, persistent directory with space for two complete source builds",
        )
        command.add_argument(
            "--plan",
            type=Path,
            default=SUPPORT_DIR / "full.json",
            help="Explicit inventory-aware test plan",
        )
        command.add_argument(
            "--therock-repository",
            required=True,
            help="Public OWNER/REPO containing the pinned TheRock source",
        )
        command.add_argument(
            "--therock-ref", required=True, help="Exact 40-character TheRock commit"
        )
        command.add_argument(
            "--therock-checkout",
            type=Path,
            help="Optional clean local checkout at the requested commit, including unpushed commits",
        )
        command.add_argument("--source-git-dir", type=Path, help=argparse.SUPPRESS)
        command.add_argument(
            "--runtime-ref",
            default=DEFAULT_RUNTIME,
            help="Exact shared rocm-systems commit",
        )
        command.add_argument(
            "--container-image", required=True, help="No-ROCm image name@sha256:digest"
        )
        command.add_argument(
            "--gpu", required=True, help="Explicit GPU ISA target, e.g. gfx942"
        )
        command.add_argument(
            "--gpu-family",
            required=True,
            help="TheRock build/distribution family, e.g. gfx94X-dcgpu",
        )
        command.add_argument(
            "--image-metadata",
            type=Path,
            required=name == "run",
            help="Inspected image identity/provisioning labels (set by launcher)",
        )
        command.add_argument(
            "--jobs",
            type=int,
            default=16,
            help="Parallel build/fetch jobs (same for both providers)",
        )
        command.add_argument(
            "--evidence-url",
            help="Actual Actions job/artifact URL; local runs leave this unset",
        )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    if args.command == "probe-provider":
        return probe_provider()
    if args.command == "foundation":
        return foundation(args.output)
    if args.command == "workflow-configuration":
        return workflow_configuration()
    if args.command == "dispatch":
        return dispatch_worker()
    if args.command == "job-evidence":
        return job_evidence()
    if args.command == "bind-evidence":
        return bind_evidence(args.work_dir, args.evidence_url)
    if args.command == "prepare-publication":
        return prepare_publication(args.work_dir, args.evidence_url)
    if args.command == "publish-report":
        return publish_report(args.report, args.expected_sha256, args.issue_repository)
    if (
        not REPOSITORY.fullmatch(args.therock_repository)
        or not SHA.fullmatch(args.therock_ref)
        or not SHA.fullmatch(args.runtime_ref)
    ):
        raise ValueError(
            "Repository and source commits must be explicit public identities"
        )
    if (
        not (
            IMAGE.fullmatch(args.container_image)
            or LOCAL_IMAGE.fullmatch(args.container_image)
        )
        or not re.fullmatch(r"gfx[0-9a-f]+", args.gpu)
        or not re.fullmatch(r"gfx[A-Za-z0-9-]+", args.gpu_family)
        or args.jobs < 1
    ):
        raise ValueError(
            "Specify a digest-pinned image, explicit GPU and positive parallelism"
        )
    if (
        "GITHUB_RUN_ID" in os.environ
        and args.evidence_url is None
        and args.command != "finish"
    ):
        raise ValueError("Actions runs require the actual current job evidence URL")
    plan = load_plan(args.plan)
    transition = Transition(args, plan)
    if args.command == "container":
        if transition.work.exists():
            raise ValueError(
                "Use a new work directory; builds and results are never reused"
            )
        try:
            result = launch_container(args, plan)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            initialize_missing(transition)
            for role in PROVIDERS:
                transition.failure("prerequisites", role, error)
            result = 1
        finally:
            initialize_missing(transition)
        report_result = finish(transition)
        return result or report_result
    if args.command == "finish":
        return finish(transition)

    verify_orchestration_snapshot(transition.work, args.therock_ref)

    # Actions cancellation first sends SIGINT; local/docker SIGTERM uses the
    # same cleanup path. Hard kill is covered by the last atomic checkpoint.
    def cancelled(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    result = transition.run()
    report_result = finish(transition)
    return result or report_result


if __name__ == "__main__":
    sys.exit(main())
