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

"""Publish a compact GitHub Actions summary without changing runner exit policy."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys


GRADES = ("OK", "LOSSY", "EMPTY", "FAIL", "SKIP")


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render(report, artifact_url=""):
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise ValueError("unsupported or missing interop results schema")
    rows = [
        ("Preparation", list(report["preparations"].values())),
        ("Source import / validation", report["sources"]),
        ("Tier A exports", [case for case in report["cases"] if case["tier"] == "A"]),
        ("Tier B returns", [case for case in report["cases"] if case["tier"] == "B"]),
        ("Metric bindings", report["regressions"]),
        ("Document contracts", report["document_regressions"]),
    ]
    lines = [
        "## Interop matrix",
        "",
        "Converter findings are **report-only**. Harness, configuration, test, and publication errors fail this job.",
        "",
        "| Check | " + " | ".join(GRADES) + " |",
        "|---|" + "---:|" * len(GRADES),
    ]
    for label, results in rows:
        counts = Counter(result["grade"] for result in results)
        if counts.keys() - set(GRADES):
            raise ValueError(f"unknown result grade in {label}")
        lines.append(
            "| "
            + label
            + " | "
            + " | ".join(str(counts[grade]) for grade in GRADES)
            + " |"
        )
    metadata = report["metadata"]
    lines += [
        "",
        f"Converters: {len(metadata['converters'])}. Harness errors: {len(report['harness_errors'])}.",
        "",
    ]
    if artifact_url:
        lines += [f"[Download report and complete evidence]({artifact_url})", ""]
    else:
        lines += [
            "Evidence download link unavailable; check the artifact upload step.",
            "",
        ]
    lines += [
        "Extract the artifact and open `report.md` for the full matrix and relative evidence links. "
        "`results.json`, the manifest snapshot, native exports, returned Ossie documents, regression oracles, "
        "and command logs are included.",
        "",
        "OK covers only the checks performed. Loss findings and optional native-gate prerequisites remain in the full report.",
        "",
    ]
    if report["harness_errors"]:
        lines += ["### Harness errors", ""]
        lines += [f"- {_cell(error)}" for error in report["harness_errors"]]
        lines += [""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="interop run directory")
    parser.add_argument(
        "--summary", required=True, type=Path, help="GITHUB_STEP_SUMMARY destination"
    )
    parser.add_argument("--artifact-url", default="")
    args = parser.parse_args(argv)
    status = 0
    try:
        report = json.loads((args.output / "results.json").read_text(encoding="utf-8"))
        summary = render(report, args.artifact_url)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        summary = (
            "## Interop matrix\n\n"
            "The machine-readable report is missing or invalid. Check the runner and artifact upload logs.\n\n"
            f"Detail: {_cell(error)}\n"
        )
        print(f"Cannot publish interop results: {error}", file=sys.stderr)
        status = 2
    try:
        with args.summary.open("a", encoding="utf-8") as destination:
            destination.write(summary + "\n")
    except OSError as error:
        print(f"Cannot write Actions summary: {error}", file=sys.stderr)
        return 2
    return status


if __name__ == "__main__":
    sys.exit(main())
