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

import json
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


def _diagnostic(converter, message, code=None, **details):
    return {
        **details,
        "converter": converter,
        "code": code,
        "message": message,
        "foreign_extension": bool(
            re.search(
                r"foreign.*(?:extension|vendor)|(?:extension|vendor).*foreign|"
                r"(?:unknown|unsupported|ignor\w*|dropp\w*).*custom_extensions?|"
                r"custom_extensions?.*(?:unknown|unsupported|ignor\w*|dropp\w*)",
                message,
                re.I,
            )
        ),
    }


def _thoughtspot_issues(text, converter):
    """Decode complete JSON arrays, leaving unrelated stderr for the fallback."""
    found, remainder, end = [], [], 0
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^[ \t]*\[", text):
        start = match.end() - 1
        if start < end:
            continue
        try:
            issues, stop = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        if not isinstance(issues, list) or any(
            not isinstance(issue, dict)
            or not isinstance(issue.get("message"), str)
            or issue.get("severity") not in ("INFO", "WARNING", "ERROR")
            for issue in issues
        ):
            continue
        remainder.append(text[end:start])
        # Keep every issue property (including object_ref/remedy and future keys).
        found.extend(
            {**issue, **_diagnostic(converter, issue["message"], issue.get("code"))}
            for issue in issues
        )
        end = stop
    remainder.append(text[end:])
    return found, "\n".join(remainder)


def has_degradation(issues):
    # Informational issues remain evidence without turning a successful stage LOSSY.
    return any(issue.get("severity", "WARNING") != "INFO" for issue in issues)


def diagnostics(result, converter):
    """One complete issue per converter diagnostic, with a text fallback."""
    text = Path(result["stderr"]).read_text(encoding="utf-8", errors="replace")
    found = []
    if converter == "thoughtspot":
        found, text = _thoughtspot_issues(text, converter)
    notices = False
    block_indent = None
    previous_indent = None
    for line in text.splitlines():
        if converter == "orionbelt":
            # CLI conversion warnings and validation warnings are separate blocks.
            if re.fullmatch(r"Conversion warnings:|Warnings: *[0-9]+", line.strip()):
                block_indent = len(line) - len(line.lstrip())
                previous_indent = None
                continue
            if block_indent is not None:
                indent = len(line) - len(line.lstrip())
                if not line.strip():
                    continue
                if indent == block_indent + 2 and line.lstrip().startswith("- "):
                    found.append(
                        _diagnostic(converter, line.lstrip()[2:], severity="WARNING")
                    )
                    previous_indent = indent
                    continue
                if previous_indent is not None and indent > previous_indent:
                    found[-1]["message"] += "\n" + line.strip()
                    # A continuation can itself explain extension loss.
                    found[-1] = _diagnostic(
                        converter,
                        **{
                            key: value
                            for key, value in found[-1].items()
                            if key not in ("converter", "foreign_extension")
                        },
                    )
                    continue
                block_indent = previous_indent = None
        if converter == "microsoft":
            scoped = re.match(r"^\[([^\]]+)\]\s+(.+)", line)
            if scoped:
                found.append(
                    _diagnostic(
                        converter,
                        line.strip(),
                        scope=scoped.group(1),
                        severity="WARNING",
                    )
                )
                continue
        if line.startswith("Conversion notices ("):
            notices = True
            continue
        if re.match(r"\s*warnings\.warn\b", line):
            continue
        code = re.match(r"\s*\[([A-Z_]+)\]\s*(.*)", line)
        warning = re.search(r"\bwarn(?:ing)?s?\b|\b\w+Warning:", line, re.I)
        if not (code or warning or (notices and line.startswith("  "))):
            continue
        typed_warning = re.match(r"\s*\[WARNING\]\s+([A-Z_]+):", line)
        found.append(
            _diagnostic(
                converter,
                line.strip(),
                code=typed_warning.group(1)
                if typed_warning
                else code.group(1)
                if code
                else None,
                severity=code.group(1)
                if code and code.group(1) in ("INFO", "WARNING", "ERROR")
                else "WARNING",
            )
        )
    return found
