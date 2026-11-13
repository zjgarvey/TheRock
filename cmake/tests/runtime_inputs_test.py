#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Rust inputs invalidate the real producer/dependent fingerprints and stamps."""

from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RuntimeInputsTest(unittest.TestCase):
    def test_config_selection_content_and_tool_changes_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            build = source / "build"
            cargo = source / "cargo"
            rustc = source / "rustc"
            config = source / "cargo.toml"
            alternative = source / "other.toml"
            for tool in (cargo, rustc):
                tool.write_text("#!/bin/sh\nexit 0\n")
                tool.chmod(0o755)
            config.write_text("first\n")
            alternative.write_text("alternative\n")
            for name in ("NOTICE-one", "NOTICE-two"):
                (source / name).write_text("fixture notice\n")
            for name in ("producer", "consumer"):
                directory = source / name
                directory.mkdir()
                content = (
                    'file(READ "${ROCM_RUNTIMES_CARGO_CONFIG}" value)\n'
                    "list(LENGTH ROCM_RUNTIMES_LICENSE_FILES notice_count)\n"
                    'if(NOT notice_count EQUAL 2)\nmessage(FATAL_ERROR "Lost notice inputs")\nendif()\n'
                    if name == "producer"
                    else 'set(value "consumer")\n'
                )
                (directory / "CMakeLists.txt").write_text(
                    "cmake_minimum_required(VERSION 3.25)\nproject(fixture NONE)\n"
                    + content
                    + f'file(WRITE "${{CMAKE_BINARY_DIR}}/{name}.txt" "${{value}}")\n'
                    + f'install(FILES "${{CMAKE_BINARY_DIR}}/{name}.txt" DESTINATION share)\n'
                )
            (source / "CMakeLists.txt").write_text(
                f"""
cmake_minimum_required(VERSION 3.25)
project(runtime_inputs NONE)
set(THEROCK_SOURCE_DIR "{ROOT.as_posix()}")
set(THEROCK_BINARY_DIR "${{CMAKE_BINARY_DIR}}")
list(APPEND CMAKE_MODULE_PATH "${{THEROCK_SOURCE_DIR}}/cmake")
find_package(Python3 COMPONENTS Interpreter REQUIRED)
include(therock_globals)
include(therock_sanitizers)
include(therock_flag_utils)
include(therock_default_targets)
include(therock_subproject)
include(therock_runtime_inputs)
set(ROCM_BUILD_FLAGS_STATE_FILE "${{CMAKE_BINARY_DIR}}/flags.cmake")
file(WRITE "${{ROCM_BUILD_FLAGS_STATE_FILE}}" "# fixture\\n")
set(ROCM_RUNTIMES_CARGO "{cargo.as_posix()}")
set(ROCM_RUNTIMES_RUSTC "{rustc.as_posix()}")
set(ROCM_RUNTIMES_LICENSE_FILES "{source.as_posix()}/NOTICE-one;{source.as_posix()}/NOTICE-two")
therock_prepare_runtime_inputs(args state depends)
therock_cmake_subproject_declare(producer
  EXTERNAL_SOURCE_DIR "${{CMAKE_CURRENT_SOURCE_DIR}}/producer"
  BINARY_DIR "${{CMAKE_BINARY_DIR}}/producer"
  FPRINT_SOURCE_HASH
  FPRINT_FILE_GLOBS "${{state}}"
  EXTRA_DEPENDS ${{depends}}
  CMAKE_ARGS ${{args}}
)
therock_cmake_subproject_activate(producer)
therock_cmake_subproject_declare(consumer
  EXTERNAL_SOURCE_DIR "${{CMAKE_CURRENT_SOURCE_DIR}}/consumer"
  BINARY_DIR "${{CMAKE_BINARY_DIR}}/consumer"
  FPRINT_SOURCE_HASH
  RUNTIME_DEPS producer
)
therock_cmake_subproject_activate(consumer)
get_target_property(producer_fingerprint producer THEROCK_FPRINT)
get_target_property(consumer_fingerprint consumer THEROCK_FPRINT)
file(WRITE "${{CMAKE_BINARY_DIR}}/fingerprints.txt"
  "${{producer_fingerprint}}\\n${{consumer_fingerprint}}\\n")
"""
            )

            def run(*args):
                result = subprocess.run(
                    ["cmake", *map(str, args)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            def fingerprints():
                values = (build / "fingerprints.txt").read_text().splitlines()
                self.assertEqual(len(values), 2)
                self.assertTrue(all(len(value) == 64 for value in values))
                return values

            run(
                "-S",
                source,
                "-B",
                build,
                "-GNinja",
                f"-DROCM_RUNTIMES_CARGO_CONFIG={config}",
            )
            run("--build", build, "--target", "consumer")
            before = fingerprints()
            self.assertEqual(
                (build / "consumer/dist/share/producer.txt").read_text(), "first\n"
            )

            # Same-path edits trigger outer regeneration and both downstream stamps.
            # CMake's fixture file installation compares whole-second times.
            # Keep each generated payload in a later second before checking dist.
            time.sleep(1.05)
            config.write_text("changed configuration\n")
            run("--build", build, "--target", "consumer")
            after = fingerprints()
            self.assertTrue(all(a != b for a, b in zip(before, after)))
            self.assertEqual(
                (build / "consumer/dist/share/producer.txt").read_text(),
                "changed configuration\n",
            )

            time.sleep(1.05)
            run(
                "-S", source, "-B", build, f"-DROCM_RUNTIMES_CARGO_CONFIG={alternative}"
            )
            selected = fingerprints()
            self.assertTrue(all(a != b for a, b in zip(after, selected)))
            run("--build", build, "--target", "consumer")
            self.assertEqual(
                (build / "consumer/dist/share/producer.txt").read_text(),
                "alternative\n",
            )

            cargo.write_text("#!/bin/sh\n# changed tool implementation\nexit 0\n")
            run("--build", build, "--target", "consumer")
            tool_changed = fingerprints()
            self.assertTrue(all(a != b for a, b in zip(selected, tool_changed)))


if __name__ == "__main__":
    unittest.main()
