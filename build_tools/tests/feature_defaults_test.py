#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Exercise generated experimental feature defaults through CMake."""

from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _therock_utils.build_topology import BuildTopology
from topology_to_cmake import generate_feature_declarations
from configure_stage import get_stage_features, get_artifact_features

REPO_ROOT = Path(__file__).resolve().parents[2]


class FeatureDefaultsTest(unittest.TestCase):
    def test_defaults_cache_overrides_and_platform_restrictions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            topology_path = root / "BUILD_TOPOLOGY.toml"
            topology_path.write_text(
                '[build_stages.runtime]\nartifact_groups = ["core"]\n'
                "[artifact_groups.core]\n"
                '[artifacts.experimental]\nartifact_group = "core"\n'
                'feature_group = "ALL"\nfeature_name = "EXPERIMENTAL"\ndefault_enabled = false\n'
                'disable_platforms = ["windows"]\n'
                '[artifacts.normal]\nartifact_group = "core"\n'
                'feature_group = "ALL"\nfeature_name = "NORMAL"\n'
            )
            topology = BuildTopology(topology_path)
            self.assertNotIn("EXPERIMENTAL", get_stage_features(topology, "runtime"))
            self.assertIn(
                "EXPERIMENTAL", get_artifact_features(topology, ["experimental"])
            )
            topology.artifacts["normal"].artifact_deps = ["experimental"]
            self.assertIn("EXPERIMENTAL", get_stage_features(topology, "runtime"))
            topology.artifacts["normal"].artifact_deps = []
            generated = StringIO()
            generate_feature_declarations(topology, generated)
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.25)\nproject(defaults NONE)\n"
                f'include("{REPO_ROOT.as_posix()}/cmake/therock_features.cmake")\n'
                "set(THEROCK_ENABLE_ALL ON)\n"
                + generated.getvalue()
                + '\nfile(WRITE "${CMAKE_BINARY_DIR}/effective.txt" '
                '"${THEROCK_ENABLE_EXPERIMENTAL};${THEROCK_ENABLE_NORMAL}")\n'
            )
            build = root / "build"

            def configure(*args):
                return subprocess.run(
                    ["cmake", "-S", str(root), "-B", str(build), *args],
                    capture_output=True,
                    text=True,
                )

            result = configure()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((build / "effective.txt").read_text(), "OFF;ON")
            result = configure("-DTHEROCK_ENABLE_EXPERIMENTAL=ON")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((build / "effective.txt").read_text(), "ON;ON")
            result = configure("-DTHEROCK_RESET_FEATURES=ON")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((build / "effective.txt").read_text(), "OFF;ON")
            # An explicit default must never override a platform restriction.
            build = root / "windows"
            result = configure(
                "-DCMAKE_SYSTEM_NAME=Windows",
                "-DTHEROCK_ENABLE_EXPERIMENTAL=ON",
                "-DTHEROCK_RESET_FEATURES=OFF",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not supported", result.stdout + result.stderr)

    def test_reject_non_boolean_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "BUILD_TOPOLOGY.toml"
            path.write_text('[artifacts.example]\ndefault_enabled = "false"\n')
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                BuildTopology(path)


if __name__ == "__main__":
    unittest.main()
