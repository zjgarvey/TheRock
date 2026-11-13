# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Exercise the packaging adapters with a real ELF producer and installed consumer.

The small native fixture models upstream's imported image and test interfaces;
it does not qualify the Rust implementation's ABI or device behavior.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCER = REPO_ROOT / "core/rocm-runtimes"
PROVIDER = REPO_ROOT / "core/rust-hsa"
HEADERS = """
hsa.h hsa_ext_amd.h hsa_ext_image.h hsa_ext_finalize.h
hsa_api_trace.h hsa_api_trace_version.h hsa_amd_tool.h
hsa_ven_amd_loader.h hsa_ven_amd_aqlprofile.h hsa_ven_amd_pc_sampling.h
hsa_ext_amd_aie.h amd_hsa_common.h amd_hsa_elf.h amd_hsa_kernel_code.h
amd_hsa_queue.h amd_hsa_signal.h amd_launch_descriptor.h Brig.h
""".split()
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux ELF adapter")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(*args: str | Path, success: bool = True) -> str:
    result = subprocess.run(
        [str(arg) for arg in args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert (result.returncode == 0) == success, result.stdout
    return result.stdout


def _configure(source: Path, build: Path, *args: str, success: bool = True) -> str:
    return _run(
        "cmake", "-G", "Ninja", "-S", source, "-B", build, *args, success=success
    )


def _headers(path: Path, marker: int) -> None:
    for name in HEADERS:
        _write(path / name, "/* Header fixture. */\n")
    _write(
        path / "hsa.h",
        f"#define HEADER_ORIGIN {marker}\nint hsa_fixture(void);\n",
    )
    _write(path / "hsa_api_trace.h", '#include "inc/hsa_ext_amd.h"\n')
    _write(path / "hsa_ext_amd.h", '#include "amd_launch_descriptor.h"\n')


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    source = tmp_path / "upstream source"
    _headers(source / "api-headers/include/hsa", 1)
    _write(source / "Cargo.lock", "# Fixture dependency lock\n")
    _write(source / "rust-toolchain.toml", '[toolchain]\nchannel = "1.98.0"\n')
    _write(source / "image.c", "int hsa_fixture(void) { return 42; }\n")
    _write(source / "versions.map", "ROCR_1 { global: hsa_fixture; local: *; };\n")
    _write(
        source / "smoke.c",
        "int hsa_fixture(void);\nint main(void) { return hsa_fixture() != 42; }\n",
    )
    # The dependency fixture accepts exactly the documented formatting command.
    _write(
        source / "cargo.py",
        "import sys\nassert sys.argv[1:] == ['fmt', '--all', '--check']\n",
    )
    _write(
        source / "CMakeLists.txt",
        """
cmake_minimum_required(VERSION 3.25)
project(upstream_fixture LANGUAGES C)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
include(CTest)
add_library(fixture_image SHARED image.c)
set_target_properties(fixture_image PROPERTIES
  OUTPUT_NAME hsa-runtime64 VERSION 1.21.0 SOVERSION 1
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/lib")
target_link_options(fixture_image PRIVATE
  "-Wl,--version-script=${CMAKE_CURRENT_SOURCE_DIR}/versions.map")
add_library(runtime_shared_image SHARED IMPORTED GLOBAL)
set_target_properties(runtime_shared_image PROPERTIES
  IMPORTED_LOCATION "${CMAKE_CURRENT_BINARY_DIR}/lib/libhsa-runtime64.so.1.21.0"
  IMPORTED_SONAME libhsa-runtime64.so.1)
add_library(fixture_amdf STATIC image.c)
set_target_properties(fixture_amdf PROPERTIES OUTPUT_NAME amdf
  ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/lib")
add_library(runtime_amdf_static STATIC IMPORTED GLOBAL)
set_target_properties(runtime_amdf_static PROPERTIES
  IMPORTED_LOCATION "${CMAKE_CURRENT_BINARY_DIR}/lib/libamdf.a"
  INTERFACE_LINK_DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/lib/runtime_amdf_static-native-libs.rsp")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/lib/runtime_amdf_static-native-libs.rsp" "")
add_custom_target(rocm_runtime_rust DEPENDS fixture_image fixture_amdf)
if(BUILD_TESTING)
  add_executable(hsa_smoke smoke.c)
  target_link_libraries(hsa_smoke PRIVATE fixture_image)
  foreach(name amdf_smoke_shared amdf_smoke_static amdf_layout)
    add_custom_target(${name} DEPENDS hsa_smoke)
  endforeach()
  add_test(NAME fixture.host COMMAND hsa_smoke)
  set_tests_properties(fixture.host PROPERTIES LABELS host)
  add_test(NAME fixture.gpu COMMAND "${CMAKE_COMMAND}" -E false)
  set_tests_properties(fixture.gpu PROPERTIES LABELS gpu)
endif()
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/runtime-rust-config.cmake"
  "set(_runtime_cargo_command \\\"${Python3_EXECUTABLE}\\\" \\\"${CMAKE_CURRENT_SOURCE_DIR}/cargo.py\\\")\\n")
# An upstream install must never leak through the wrapper's private stage.
install(CODE "message(FATAL_ERROR \\\"upstream install escaped\\\")")
""",
    )
    return source


def _produce(tmp_path: Path, upstream: Path, testing: bool = False) -> Path:
    build = tmp_path / "producer-build"
    stage = tmp_path / "producer-stage"
    notice = tmp_path / "dependency-NOTICE.txt"
    _write(notice, "Dependency attribution fixture.\n")
    output = _configure(
        PRODUCER,
        build,
        f"-DROCM_RUNTIMES_SOURCE_DIR={upstream}",
        f"-DTHEROCK_BUILD_TESTING={'ON' if testing else 'OFF'}",
        f"-DROCM_RUNTIMES_LICENSE_FILES={notice}",
    )
    assert "CTS integration: unavailable" in output
    _run("cmake", "--build", build)
    if testing:
        output = _run("cmake", "--build", build, "--target", "rocm-runtimes-host-tests")
        assert "100% tests passed" in output
        assert "fixture.gpu" not in output
        assert "fixture.host" in (build / "rocm-runtimes-host-tests.xml").read_text()
    else:
        assert "BUILD_TESTING:BOOL=OFF" in (build / "CMakeCache.txt").read_text()
        assert not (build / "upstream/hsa_smoke").exists()
    _run("cmake", "--build", build, "--target", "rocm-runtimes-format-check")
    _run("cmake", "--install", build, "--prefix", stage)
    return stage


def test_private_producer_and_relocated_installed_consumer(
    tmp_path: Path, upstream: Path
) -> None:
    stage = _produce(tmp_path, upstream, testing=True)
    assert {p.name for p in stage.iterdir()} == {"lib"}
    assert {p.name for p in (stage / "lib").iterdir()} == {"rocm-runtimes"}
    assert not list(stage.rglob("*config.cmake"))
    metadata = json.loads(
        (stage / "lib/rocm-runtimes/share/rocm-runtimes/runtime.json").read_text()
    )
    assert metadata == {
        "schema_version": 1,
        "hsa_version": "1.21.0",
        "hsa_soname": "libhsa-runtime64.so.1",
        "hsa_image": "libhsa-runtime64.so.1.21.0",
        "amdf_static": "libamdf.a",
        "amdf_shared": "libamdf.so",
        "tests": {
            "host_enabled": True,
            "format_check_available": True,
            "cts_available": False,
        },
    }
    private = stage / "lib/rocm-runtimes"
    assert (private / "share/doc/rocm-runtimes/LICENSE").is_file()
    assert (private / "share/doc/rocm-runtimes/LICENSE-HSA.txt").is_file()
    assert os.readlink(private / "lib/libamdf.so") == "libamdf.so.0"
    assert os.readlink(private / "lib/libamdf.so.0") == metadata["hsa_soname"]
    assert (private / "lib/libamdf.so").samefile(private / "lib/libhsa-runtime64.so")
    _run(
        "cc",
        upstream / "smoke.c",
        private / "lib/libamdf.a",
        f"-Wl,@{private}/lib/runtime_amdf_static-native-libs.rsp",
        "-o",
        tmp_path / "static-consumer",
    )
    _run(tmp_path / "static-consumer")

    # A candidate build must coexist with an installed canonical baseline.
    prefix = tmp_path / "installed"
    _write(prefix / "lib/libhsa-runtime64.so.1", "baseline\n")
    _run("cmake", "--install", tmp_path / "producer-build", "--prefix", prefix)
    assert (prefix / "lib/libhsa-runtime64.so.1").read_text() == "baseline\n"

    # A complete candidate header tree wins even if a fallback was supplied.
    fallback = tmp_path / "fallback"
    _headers(fallback, 2)
    _configure(
        PROVIDER,
        tmp_path / "provider-build",
        f"-DROCM_RUNTIMES_DIST_DIR={stage}",
        f"-DHSA_HEADERS_SOURCE_DIR={fallback}",
    )
    _run("cmake", "--build", tmp_path / "provider-build")
    _run("cmake", "--install", tmp_path / "provider-build", "--prefix", prefix)
    assert (prefix / "share/doc/rust-hsa/LICENSE").is_file()
    assert (prefix / "share/doc/rust-hsa/LICENSE-HSA.txt").is_file()
    assert (prefix / "share/doc/rust-hsa/dependency-NOTICE.txt").read_text() == (
        "Dependency attribution fixture.\n"
    )
    assert not (prefix / "lib/libamdf.so").exists()
    assert os.readlink(prefix / "lib/libhsa-runtime64.so") == "libhsa-runtime64.so.1"
    assert os.readlink(prefix / "lib/libhsa-runtime64.so.1") == metadata["hsa_image"]
    dynamic = _run("readelf", "--dynamic", prefix / "lib/libhsa-runtime64.so")
    assert "[libhsa-runtime64.so.1]" in dynamic

    relocated = tmp_path / "relocated sdk"
    prefix.rename(relocated)
    # Remove access to the input artifacts: the consumer must use only its SDK.
    stage.rename(tmp_path / "hidden-producer")
    consumer = tmp_path / "consumer"
    _write(
        consumer / "CMakeLists.txt",
        """
cmake_minimum_required(VERSION 3.25)
project(consumer LANGUAGES C)
find_package(hsa-runtime64 1.11 REQUIRED CONFIG)
find_package(hsa-runtime64 1.21.0 EXACT REQUIRED CONFIG)
if(NOT hsa-runtime64_VERSION STREQUAL "1.21.0")
  message(FATAL_ERROR "Incorrect public HSA version")
endif()
add_executable(consumer main.c)
target_link_libraries(consumer PRIVATE hsa-runtime64::hsa-runtime64)
""",
    )
    _write(
        consumer / "main.c",
        "#include <hsa/hsa.h>\n"
        "#include <hsa/hsa_api_trace.h>\n"
        '_Static_assert(HEADER_ORIGIN == 1, "candidate headers");\n'
        "int main(void) { return hsa_fixture() != 42; }\n",
    )
    _configure(
        consumer, tmp_path / "consumer-build", f"-DCMAKE_PREFIX_PATH={relocated}"
    )
    _run("cmake", "--build", tmp_path / "consumer-build")
    _run(tmp_path / "consumer-build/consumer")
    assert "ROCR_1" in _run(
        "readelf", "--version-info", tmp_path / "consumer-build/consumer"
    )
    for config in (relocated / "lib/cmake/hsa-runtime64").iterdir():
        assert str(tmp_path) not in config.read_text()
    # Same-major compatible does not mean an older image satisfies newer APIs.
    _write(
        consumer / "CMakeLists.txt",
        "cmake_minimum_required(VERSION 3.25)\nproject(newer LANGUAGES C)\n"
        "find_package(hsa-runtime64 1.22 REQUIRED CONFIG)\n",
    )
    _configure(
        consumer,
        tmp_path / "newer-build",
        f"-DCMAKE_PREFIX_PATH={relocated}",
        success=False,
    )


def test_header_fallback_requires_a_complete_tree(
    tmp_path: Path, upstream: Path
) -> None:
    stage = _produce(tmp_path, upstream)
    metadata = json.loads(
        (stage / "lib/rocm-runtimes/share/rocm-runtimes/runtime.json").read_text()
    )
    assert metadata["tests"] == {
        "host_enabled": False,
        "format_check_available": True,
        "cts_available": False,
    }
    (stage / "lib/rocm-runtimes/include/hsa/hsa_api_trace.h").unlink()
    output = _configure(
        PROVIDER,
        tmp_path / "missing-headers",
        f"-DROCM_RUNTIMES_DIST_DIR={stage}",
        success=False,
    )
    assert "provide a complete HSA_HEADERS_SOURCE_DIR" in " ".join(output.split())
    fallback = tmp_path / "fallback"
    _headers(fallback, 2)
    _configure(
        PROVIDER,
        tmp_path / "fallback-build",
        f"-DROCM_RUNTIMES_DIST_DIR={stage}",
        f"-DHSA_HEADERS_SOURCE_DIR={fallback}",
    )
    prefix = tmp_path / "fallback-install"
    _run("cmake", "--install", tmp_path / "fallback-build", "--prefix", prefix)
    assert "HEADER_ORIGIN 2" in (prefix / "include/hsa/hsa.h").read_text()
    assert '"hsa_ext_amd.h"' in (prefix / "include/hsa/hsa_api_trace.h").read_text()


@pytest.mark.parametrize("corruption", ["metadata", "soname", "missing-image"])
def test_provider_rejects_invalid_images(
    tmp_path: Path, upstream: Path, corruption: str
) -> None:
    stage = _produce(tmp_path, upstream)
    private = stage / "lib/rocm-runtimes"
    image = private / "lib/libhsa-runtime64.so.1.21.0"
    if corruption == "metadata":
        metadata = private / "share/rocm-runtimes/runtime.json"
        payload = json.loads(metadata.read_text())
        payload["hsa_image"] = "../../outside.so"
        metadata.write_text(json.dumps(payload))
    elif corruption == "soname":
        # Real ELF with an incompatible SONAME, retaining the expected basename.
        _run(
            "cc", "-shared", "-Wl,-soname,wrong.so.2", upstream / "image.c", "-o", image
        )
    else:
        image.unlink()
    output = _configure(
        PROVIDER,
        tmp_path / "invalid-build",
        f"-DROCM_RUNTIMES_DIST_DIR={stage}",
        success=False,
    )
    assert {
        "metadata": "metadata",
        "soname": "ELF SONAME",
        "missing-image": "real versioned HSA image",
    }[corruption] in output


def test_cts_request_fails_explicitly(tmp_path: Path, upstream: Path) -> None:
    output = _configure(
        PRODUCER,
        tmp_path / "cts-build",
        f"-DROCM_RUNTIMES_SOURCE_DIR={upstream}",
        "-DROCM_RUNTIMES_BUILD_CTS=ON",
        success=False,
    )
    assert "has no CTS integration" in " ".join(output.split())


@pytest.mark.parametrize("check", ["host-tests", "format-check"])
def test_checks_propagate_failures(tmp_path: Path, upstream: Path, check: str) -> None:
    _produce(tmp_path, upstream, testing=True)
    if check == "host-tests":
        _write(upstream / "image.c", "int hsa_fixture(void) { return 0; }\n")
    else:
        _write(upstream / "cargo.py", "raise SystemExit(1)\n")
    _run(
        "cmake",
        "--build",
        tmp_path / "producer-build",
        "--target",
        f"rocm-runtimes-{check}",
        success=False,
    )
