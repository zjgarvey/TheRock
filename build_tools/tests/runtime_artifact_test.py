# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Keep HSA development files, libraries and notices in their SDK components."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _therock_utils.artifact_builder import ArtifactDescriptor, ComponentScanner

ROOT = Path(__file__).resolve().parents[2]


class RuntimeArtifactTest(unittest.TestCase):
    def classify(self, descriptor, payload):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in payload:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n")
        artifact = ArtifactDescriptor.load_toml_file(
            ROOT / "core" / descriptor, artifact_name="fixture"
        )
        scanner = ComponentScanner(root, artifact)
        results = {}
        for name, component in scanner.components.items():
            output = root / "outputs" / name
            output.mkdir(parents=True)
            component.write_artifact(output)
            results[name] = {
                p.relative_to(output).as_posix()
                for p in output.rglob("*")
                if p.is_file() and p.name != "artifact_manifest.txt"
            }
        return results

    def test_canonical_headers_notices_and_tools_have_distinct_owners(self):
        library = "core/rust-hsa/stage/lib/libhsa-runtime64.so.1"
        headers = {
            "core/rust-hsa/stage/include/hsa/hsa.h",
            "core/rust-hsa/stage/lib/cmake/hsa-runtime64/hsa-runtime64-config.cmake",
            "core/hsakmt/stage/include/hsakmt/hsakmttypes.h",
            "core/hsakmt/stage/lib/libhsakmt.a",
        }
        notices = {
            "core/rust-hsa/stage/share/doc/rust-hsa/LICENSE",
            "core/hsakmt/stage/share/doc/hsakmt/LICENSE.md",
            "core/rocminfo/stage/share/doc/rocminfo/LICENSE",
        }
        tool = "core/rocminfo/stage/bin/rocminfo"
        stale = "core/ROCR-Runtime/stage/lib/libhsa-runtime64.so.1"
        results = self.classify(
            "artifact-core-runtime-rust.toml",
            headers | notices | {library, tool, stale},
        )
        self.assertEqual(results["lib"], {library})
        self.assertEqual(results["run"], {tool})
        self.assertEqual(results["dev"], headers)
        self.assertEqual(results["doc"], notices)
        self.assertNotIn(stale, set.union(*results.values()))

    def test_private_metadata_and_static_archive_are_development_payloads(self):
        prefix = "core/rocm-runtimes/stage/lib/rocm-runtimes/"
        library = prefix + "lib/libhsa-runtime64.so.1"
        development = {
            prefix + "lib/libamdf.a",
            prefix + "lib/runtime_amdf_static-native-libs.rsp",
            prefix + "include/amdf/amdf.h",
            prefix + "share/rocm-runtimes/runtime.json",
        }
        notice = prefix + "share/doc/rocm-runtimes/LICENSE"
        results = self.classify(
            "artifact-rocm-runtimes.toml", development | {library, notice}
        )
        self.assertEqual(results["lib"], {library})
        self.assertEqual(results["dev"], development)
        self.assertEqual(results["doc"], {notice})


if __name__ == "__main__":
    unittest.main()
