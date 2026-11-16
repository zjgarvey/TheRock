# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RuntimeFeatureTest(unittest.TestCase):
    def test_explicit_default_reset_and_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.25)\nproject(features NONE)\n"
                f'include("{ROOT}/cmake/therock_features.cmake")\n'
                'option(THEROCK_ENABLE_ALL "All" ON)\n'
                'therock_add_feature(BASE GROUP ALL DESCRIPTION "Base")\n'
                'therock_add_feature(COMPILER GROUP ALL DESCRIPTION "Compiler")\n'
                "therock_add_feature(ROCM_RUNTIMES DEFAULT OFF REQUIRES BASE COMPILER "
                'DESCRIPTION "Runtime tests")\ntherock_finalize_features()\n'
                'file(WRITE "${CMAKE_BINARY_DIR}/selected.txt" '
                '"${THEROCK_ENABLE_ROCM_RUNTIMES};${THEROCK_ENABLE_COMPILER};${THEROCK_ENABLE_BASE}")\n'
            )
            build = source / "build"
            for options, expected in (
                ([], "OFF;ON;ON"),
                (
                    [
                        "-DTHEROCK_ENABLE_ALL=OFF",
                        "-DTHEROCK_ENABLE_COMPILER=OFF",
                        "-DTHEROCK_ENABLE_ROCM_RUNTIMES=ON",
                    ],
                    "ON;ON;ON",
                ),
                (["-DTHEROCK_RESET_FEATURES=ON"], "OFF;OFF;OFF"),
            ):
                result = subprocess.run(
                    ["cmake", "-S", str(source), "-B", str(build), *options],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((build / "selected.txt").read_text(), expected)


if __name__ == "__main__":
    unittest.main()
