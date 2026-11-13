#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""CTest interposer for existing component scripts; keeps their exact selection."""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main() -> int:
    command = [os.environ["RUNTIME_TRANSITION_CTEST"], *sys.argv[1:]]
    # Scripts query labels before constructing their GPU/category selection.
    if any(arg in command for arg in ("--print-labels", "--show-only=json-v1", "-N")):
        return subprocess.call(command)
    if any(arg.startswith("--repeat") for arg in command):
        raise ValueError("Retrying CTest commands cannot qualify a transition run")
    directory = Path(os.environ["RUNTIME_TRANSITION_SUITE_OUTPUT"])
    directory.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    inventory = directory / f"{identifier}.inventory.json"
    with inventory.open("w") as output:
        subprocess.run([*command, "--show-only=json-v1"], stdout=output, check=True)
    data = json.loads(inventory.read_text())
    if not data.get("tests"):
        raise ValueError("Component selected zero CTest tests")
    return subprocess.call(
        [
            *command,
            "--no-tests=error",
            "--output-junit",
            str(directory / f"{identifier}.xml"),
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
