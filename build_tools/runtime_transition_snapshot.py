# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Bounded, retained orchestration tools from an exact locally available Git tree."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile

SHA = re.compile(r"[0-9a-f]{40}")
MAX_ORCHESTRATION_BYTES = 64 * 1024 * 1024
MAX_ORCHESTRATION_ENTRIES = 8192
ORCHESTRATION_HELPERS = {
    "build_tools/runtime_transition.py",
    "build_tools/runtime_transition_snapshot.py",
    "build_tools/runtime_transition_report.py",
    "build_tools/runtime_transition/ctest_driver.py",
    "build_tools/runtime_transition/loader_audit.c",
    "build_tools/github_actions/detect_failed_logs.py",
    "build_tools/github_actions/github_actions_api.py",
}


class SnapshotError(ValueError):
    """A snapshot cannot supply verified tools for this source identity."""


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _capture(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    return subprocess.check_output(command, cwd=cwd, env=env, text=True).strip()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def orchestration_directory(work: Path) -> Path:
    return work.parent / f".{work.name}-orchestrator"


def orchestration_files(
    archive_path: Path, expected: str, destination: Path | None = None
) -> dict[str, str]:
    """Read a bounded Git archive, accepting only build_tools files/directories."""
    if (
        not stat.S_ISREG(archive_path.lstat().st_mode)
        or archive_path.stat().st_size > MAX_ORCHESTRATION_BYTES
    ):
        raise SnapshotError("Orchestration archive is not a bounded regular file")
    files = {}
    seen = set()
    total = 0
    with tarfile.open(archive_path, "r:") as archive:
        if archive.pax_headers.get("comment") != expected:
            raise SnapshotError("Orchestration archive source commit mismatch")
        for member in archive:
            name = member.name.rstrip("/") if member.isdir() else member.name
            path = PurePosixPath(name)
            total += member.size
            if (
                not path.parts
                or path.parts[0] != "build_tools"
                or name != path.as_posix()
                or ".." in path.parts
                or "\\" in name
                or not (member.isfile() or member.isdir())
                or name in seen
                or len(seen) >= MAX_ORCHESTRATION_ENTRIES
                or member.size < 0
                or total > MAX_ORCHESTRATION_BYTES
            ):
                raise SnapshotError("Unsafe or oversized orchestration archive entry")
            seen.add(name)
            if member.isdir():
                if destination is not None:
                    (destination / name).mkdir(parents=True, exist_ok=True)
                continue
            with archive.extractfile(member) as source:
                content = source.read()
            files[name] = hashlib.sha256(content).hexdigest()
            if destination is not None:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(content)
                target.chmod(0o555 if member.mode & 0o111 else 0o444)
    if not ORCHESTRATION_HELPERS <= files.keys():
        raise SnapshotError("Selected commit lacks required orchestration helpers")
    return files


def create_orchestration_snapshot(
    work: Path, source: Path, expected: str, environment: dict[str, str]
) -> Path:
    """Pin tools from the selected commit without changing the source repository."""
    if not SHA.fullmatch(expected):
        raise SnapshotError("Orchestration requires an exact source commit")
    root = orchestration_directory(work)
    root.mkdir(parents=True, exist_ok=False)
    archive_path = root / "build-tools.tar"
    environment = {
        **environment,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        with archive_path.open("xb") as output:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.attributesFile=/dev/null",
                    "archive",
                    "--format=tar",
                    expected,
                    "build_tools",
                ],
                cwd=source,
                env=environment,
                stdout=output,
                check=True,
            )
        files = orchestration_files(archive_path, expected, root)
        # Archive attributes can omit or substitute blobs. Verify the exact
        # committed tree so repository attributes cannot silently change tools.
        entries = _capture(
            ["git", "ls-tree", "-rz", expected, "--", "build_tools"],
            cwd=source,
            env=environment,
        )
        tree = set()
        for entry in entries.split("\0"):
            if not entry:
                continue
            identity, name = entry.split("\t", 1)
            mode, kind, object_id = identity.split()
            if mode not in {"100644", "100755"} or kind != "blob" or name not in files:
                raise SnapshotError(
                    "Orchestration archive differs from the source tree"
                )
            content = (root / name).read_bytes()
            if (
                hashlib.sha1(
                    f"blob {len(content)}\0".encode() + content, usedforsecurity=False
                ).hexdigest()
                != object_id
            ):
                raise SnapshotError(
                    "Orchestration archive differs from the source tree"
                )
            tree.add(name)
        if tree != files.keys():
            raise SnapshotError("Orchestration archive differs from the source tree")
        _write_json(
            root / "snapshot.json",
            {
                "schema_version": 1,
                "source_sha": expected,
                "archive_sha256": _digest(archive_path),
                "content_sha256": hashlib.sha256(
                    json.dumps(files, sort_keys=True).encode()
                ).hexdigest(),
            },
        )
        archive_path.chmod(0o444)
        (root / "snapshot.json").chmod(0o444)
        for directory in sorted(root.rglob("*"), reverse=True):
            if directory.is_dir():
                directory.chmod(0o555)
        root.chmod(0o555)
        return verify_orchestration_snapshot(work, expected)
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise SnapshotError(
            "Cannot snapshot the requested source commit's tools"
        ) from error


def verify_orchestration_snapshot(work: Path, expected: str) -> Path:
    """Fail closed before executing retained tools, including later finalization."""
    root = orchestration_directory(work)
    try:
        metadata_path = root / "snapshot.json"
        if (
            not stat.S_ISDIR(root.lstat().st_mode)
            or not stat.S_ISREG(metadata_path.lstat().st_mode)
            or metadata_path.stat().st_size > 4096
        ):
            raise SnapshotError("Invalid orchestration snapshot metadata")
        metadata = json.loads(metadata_path.read_text())
        archive_path = root / "build-tools.tar"
        files = orchestration_files(archive_path, expected)
        if (
            metadata["schema_version"] != 1
            or metadata["source_sha"] != expected
            or metadata["archive_sha256"] != _digest(archive_path)
            or metadata["content_sha256"]
            != hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        ):
            raise SnapshotError("Orchestration snapshot provenance mismatch")
        observed = {}
        for index, path in enumerate(root.rglob("*")):
            if index >= MAX_ORCHESTRATION_ENTRIES + 2:
                raise SnapshotError("Orchestration snapshot exceeds entry limit")
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise SnapshotError("Orchestration snapshot contains a nonregular file")
            name = path.relative_to(root).as_posix()
            if name in {"snapshot.json", "build-tools.tar"}:
                continue
            if name not in files or path.stat().st_size > MAX_ORCHESTRATION_BYTES:
                raise SnapshotError("Unexpected orchestration snapshot file")
            observed[name] = _digest(path)
        if observed != files:
            raise SnapshotError("Orchestration snapshot content mismatch")
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        raise SnapshotError(
            f"Invalid retained orchestration snapshot: {error}"
        ) from error
    return root
