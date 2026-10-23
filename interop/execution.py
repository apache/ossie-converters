# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Subprocess isolation and diagnostic normalization; never execute through a shell."""

import os
from pathlib import Path
import re
import signal
import subprocess
import time


def run_command(argv: list[str], cwd: Path, evidence: Path, timeout: float) -> dict:
    evidence.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {
        "argv": argv,
        "cwd": str(cwd),
        "returncode": None,
        "error": None,
        "stdout": str(evidence / "stdout.txt"),
        "stderr": str(evidence / "stderr.txt"),
    }
    with (evidence / "stdout.txt").open("w", encoding="utf-8") as stdout, (
        evidence / "stderr.txt"
    ).open("w", encoding="utf-8") as stderr:
        try:
            # A separate process group lets a timed-out uv/Maven invocation take its
            # child processes with it. File-backed logs avoid buffering unbounded output.
            environment = os.environ.copy()
            # The harness itself may run under uv. Its active environment is not
            # the converter's environment; leaking it emits spurious warnings and
            # UV_PROJECT_ENVIRONMENT can even select the wrong dependency set.
            for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
                environment.pop(name, None)
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=(os.name == "posix"),
            )
            try:
                result["returncode"] = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                result["error"] = f"timed out after {timeout:g}s"
        except OSError as error:
            result["error"] = str(error)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def succeeded(result):
    return result["error"] is None and result["returncode"] == 0


def diagnostics(result, converter):
    """Keep stage provenance; foreign-extension counts are a conservative heuristic."""
    text = Path(result["stderr"]).read_text(encoding="utf-8", errors="replace")
    found = []
    notices = False
    for line in text.splitlines():
        if line.startswith("Conversion notices ("):
            notices = True
            continue
        if re.match(r"\s*warnings\.warn\b", line):
            continue
        code = re.match(r"\s*\[([A-Z_]+)\]\s*(.*)", line)
        warning = re.search(r"\bwarn(?:ing)?s?\b|\b\w+Warning:", line, re.I)
        if not (code or warning or (notices and line.startswith("  "))):
            continue
        message = line.strip()
        typed_warning = re.match(r"\s*\[WARNING\]\s+([A-Z_]+):", line)
        found.append(
            {
                "converter": converter,
                "code": typed_warning.group(1)
                if typed_warning
                else code.group(1)
                if code
                else None,
                "message": message,
                "foreign_extension": bool(
                    re.search(
                        r"foreign.*(?:extension|vendor)|(?:extension|vendor).*foreign|"
                        r"(?:unknown|unsupported|ignor\w*|dropp\w*).*custom_extensions?",
                        message,
                        re.I,
                    )
                ),
            }
        )
    return found
