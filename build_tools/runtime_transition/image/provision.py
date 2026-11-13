#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Install pinned host tools and prepare the selected runtime's Cargo cache."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from urllib.parse import urlparse

IMAGE_DIR = Path(__file__).resolve().parent
METADATA_DIR = IMAGE_DIR.parent
ROOT = Path("/opt/runtime-transition")
RUNTIME_ORIGIN = "https://github.com/ROCm/rocm-systems.git"


def run(*arguments: str | Path, **kwargs) -> None:
    subprocess.run([str(argument) for argument in arguments], check=True, **kwargs)


def output(*arguments: str | Path, **kwargs) -> str:
    return subprocess.check_output(
        [str(argument) for argument in arguments], text=True, **kwargs
    ).strip()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url: str, expected_sha256: str, directory: Path) -> Path:
    archive = directory / Path(urlparse(url).path).name
    run(
        "curl",
        "--fail",
        "--show-error",
        "--location",
        "--retry",
        "3",
        url,
        "--output",
        archive,
    )
    if digest(archive) != expected_sha256:
        raise ValueError(f"SHA256 mismatch for {url}")
    return archive


def install_toolchains() -> None:
    lock = json.loads((IMAGE_DIR / "downloads.json").read_text())
    with tempfile.TemporaryDirectory(prefix="runtime-transition-tools-") as temporary:
        directory = Path(temporary)
        download(lock["rust_manifest_url"], lock["rust_manifest_sha256"], directory)
        for component in lock["rust_components"]:
            archive = download(component["url"], component["sha256"], directory)
            run("tar", "--extract", "--xz", "--file", archive, "--directory", directory)
            extracted = directory / archive.name.removesuffix(".tar.xz")
            run(
                "bash",
                extracted / "install.sh",
                f"--prefix={ROOT / 'rust'}",
                "--disable-ldconfig",
            )
            shutil.rmtree(extracted)
            archive.unlink()
        node = lock["node"]
        archive = download(node["url"], node["sha256"], directory)
        (ROOT / "node").mkdir(parents=True)
        run(
            "tar",
            "--extract",
            "--xz",
            "--file",
            archive,
            "--directory",
            ROOT / "node",
            "--strip-components=1",
        )
    if output("rustc", "--version").split()[1] != lock["rust_version"]:
        raise ValueError("The installed Rust compiler does not match the download lock")
    if output("cargo", "--version").split()[1] != lock["rust_version"]:
        raise ValueError("The installed Cargo does not match the download lock")
    run("rustfmt", "--version")
    run("cargo", "clippy", "--version")
    if output("node", "--version") != f"v{lock['node']['version']}":
        raise ValueError("The installed Node.js does not match the download lock")
    run("npm", "--version")


def prepare_runtime(runtime_ref: str, lock_sha256: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", runtime_ref):
        raise ValueError("runtime-ref must be a full public commit SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", lock_sha256):
        raise ValueError("cargo-lock-sha256 must be a SHA256 digest")
    downloads = json.loads((IMAGE_DIR / "downloads.json").read_text())
    with tempfile.TemporaryDirectory(prefix="runtime-transition-source-") as temporary:
        source = Path(temporary)
        run("git", "init", "--quiet", source)
        run("git", "-C", source, "remote", "add", "origin", RUNTIME_ORIGIN)
        run("git", "-C", source, "sparse-checkout", "set", "runtimes")
        run(
            "git",
            "-C",
            source,
            "fetch",
            "--depth=1",
            "--filter=blob:none",
            "origin",
            runtime_ref,
        )
        run("git", "-C", source, "checkout", "--detach", "--quiet", "FETCH_HEAD")
        if output("git", "-C", source, "rev-parse", "HEAD") != runtime_ref:
            raise ValueError("The runtime checkout does not match runtime-ref")
        runtimes = source / "runtimes"
        lockfile = runtimes / "Cargo.lock"
        if digest(lockfile) != lock_sha256:
            raise ValueError(
                "The runtime Cargo.lock does not match the requested digest"
            )
        toolchain = tomllib.loads((runtimes / "rust-toolchain.toml").read_text())
        if toolchain["toolchain"]["channel"] != downloads["rust_version"]:
            raise ValueError("The runtime requires a different Rust toolchain")
        manifest = runtimes / "Cargo.toml"
        environment = os.environ | {"CARGO_NET_OFFLINE": "false"}
        run("cargo", "fetch", "--manifest-path", manifest, "--locked", env=environment)
        run("cargo", "fetch", "--manifest-path", manifest, "--frozen", "--offline")
        # Retain the exact dependency and toolchain inputs after discarding the checkout.
        shutil.copyfile(lockfile, METADATA_DIR / "runtime-Cargo.lock")
        shutil.copyfile(
            runtimes / "rust-toolchain.toml", METADATA_DIR / "rust-toolchain.toml"
        )
    provenance = {
        "schema_version": 1,
        "parent_image": "ghcr.io/rocm/no_rocm_image_ubuntu24_04@sha256:"
        "a13e8370a2d1d33a910e47669cdf075dbe9554685e78592f22935ec7c7c35e39",
        "runtime_origin": RUNTIME_ORIGIN,
        "runtime_ref": runtime_ref,
        "cargo_lock_sha256": lock_sha256,
        "cargo_home": os.environ["CARGO_HOME"],
        "downloads": downloads,
        "tools": {
            "rustc": output("rustc", "--version"),
            "cargo": output("cargo", "--version"),
            "rustfmt": output("rustfmt", "--version"),
            "clippy": output("cargo", "clippy", "--version"),
            "node": output("node", "--version"),
            "npm": output("npm", "--version"),
            "cmake": output("cmake", "--version").splitlines()[0],
            "ninja": output("ninja", "--version"),
            "patchelf": output("patchelf", "--version"),
        },
        "apt_packages": output(
            "dpkg-query", "-W", "-f=${binary:Package}=${Version}\n"
        ).splitlines(),
        "python_packages": json.loads(
            output("python3", "-m", "pip", "list", "--format=json")
        ),
        "requirements_sha256": digest(METADATA_DIR / "requirements.txt"),
    }
    (METADATA_DIR / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("toolchains")
    runtime = commands.add_parser("runtime")
    runtime.add_argument("--runtime-ref", required=True)
    runtime.add_argument("--cargo-lock-sha256", required=True)
    arguments = parser.parse_args()
    if arguments.command == "toolchains":
        install_toolchains()
    else:
        prepare_runtime(arguments.runtime_ref, arguments.cargo_lock_sha256)


if __name__ == "__main__":
    main()
