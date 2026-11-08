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

"""Report-only portability scores: grade each round trip with the core grader.

The portability fixtures and the grader live in the core repository
(`ossie/compliance/portability/`, apache/ossie#596). Every fixture variant is
exported to each round-trippable converter and re-imported by the same runner
code as Tier A/B, then the returned document is graded against its input, element
by element. The scores are reported next to the matrix and never change the exit
status, with or without --strict.
"""

from functools import cache
import importlib.util
from pathlib import Path
import sys

GRADER = Path("ossie/compliance/portability/grader/grade.py")

# Metric columns of the portability table, in order.
# `other` is an outcome this table does not know yet (a newer grader), kept visible.
COLUMNS = (
    "portable",
    "review",
    "vendor",
    "placeholder",
    "dropped",
    "rejected",
    "added",
    "other",
)

# Same text or a cosmetic rewrite of the authored portable expression.
_PORTABLE = {"PORTABLE", "RELABELED"}
_DROPPED = {"DROPPED_DIAGNOSED", "DROPPED_SILENT"}


@cache
def load_grader(path: Path):
    """The core grader module from the pinned submodule (loaded once per path)."""
    spec = importlib.util.spec_from_file_location("interop_portability_grader", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the portability grader from {path}")
    module = importlib.util.module_from_spec(spec)
    # The grader defines dataclasses, which look their module up in sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


def diagnostics_text(commands):
    """The stdout and stderr of a case's commands, for DROPPED_DIAGNOSED vs SILENT."""
    parts = []
    for stage in commands:
        for key in ("stdout", "stderr"):
            path = stage.get(key)
            if path and Path(path).is_file():
                parts.append(Path(path).read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def as_dict(report):
    """A grader report as JSON-ready data, plus the metric columns."""
    findings = [vars(finding).copy() for finding in report.findings]
    return {
        "rejected": report.rejected,
        "findings": findings,
        "summary": report.counts(),
        "metrics": tally(findings),
    }


def tally(findings):
    """Fold one report's metric findings into the table columns.

    ADDED metrics are invented by the converter, so they are counted on their own
    and not in `authored`. A REWRITTEN metric is portable when the grader proved
    the normalized trees equal, otherwise it needs review.
    """
    counts = dict.fromkeys(COLUMNS + ("authored",), 0)
    for finding in findings:
        if finding["kind"] != "metric":
            continue
        outcome = finding["outcome"]
        if outcome == "ADDED":
            counts["added"] += 1
            continue
        counts["authored"] += 1
        if outcome in _PORTABLE:
            counts["portable"] += 1
        elif outcome == "REWRITTEN":
            cosmetic = finding.get("detail", "").startswith("cosmetic")
            counts["portable" if cosmetic else "review"] += 1
        elif outcome == "SURVIVED_VENDOR_ONLY":
            counts["vendor"] += 1
        elif outcome == "PLACEHOLDER":
            counts["placeholder"] += 1
        elif outcome in _DROPPED:
            counts["dropped"] += 1
        elif outcome == "REJECTED":
            counts["rejected"] += 1
        else:
            counts["other"] += 1
    return counts
