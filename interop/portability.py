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
import re
import sys

GRADER = Path("ossie/compliance/portability/grader/grade.py")

# Columns of the portability tables, in order. `other` is an outcome, or a grader
# detail, this table cannot read (a newer grader): kept visible, never misfiled.
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

# The element kinds the grader reports, each totalled in its own table.
KINDS = ("metric", "field")

# The grader's PORTABLE_DIALECTS (a test keeps the two equal).
PORTABLE_DIALECTS = frozenset({"ANSI_SQL", "OSSIE_SQL_2026"})

_COLUMN = {
    "PORTABLE": "portable",
    "SURVIVED_VENDOR_ONLY": "vendor",
    "PLACEHOLDER": "placeholder",
    "DROPPED_DIAGNOSED": "dropped",
    "DROPPED_SILENT": "dropped",
    "REJECTED": "rejected",
}

# The grader says which tag a RELABELED element came back under, and why a
# REWRITTEN one needs review, only in its `detail` text. The tests grade real
# documents with the pinned grader, so a rewording fails them on a submodule bump.
_RELABELED = re.compile(r"authored \S+, returned (\S+)")
_REWRITTEN = (
    ("cosmetic", "portable"),
    ("review", "review"),
    ("identity not preserved", "review"),
)

# Keys the harness adds to a diagnostic; the others are the converter's own words.
_HARNESS_KEYS = {"converter", "stage", "foreign_extension"}


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


def diagnostics_text(diagnostics):
    """The converter's own diagnostics in a case, for DROPPED_DIAGNOSED vs SILENT.

    These are the diagnostics the matrix reports, not the raw command output:
    success messages and file paths would otherwise diagnose any element whose name
    they contain. Every key the converter wrote is kept (thoughtspot names the
    element in `object_ref`, not in `message`)."""
    return "\n".join(
        str(value)
        for diagnostic in diagnostics
        for key, value in diagnostic.items()
        if key not in _HARNESS_KEYS and value is not None
    )


def as_dict(report):
    """A grader report as JSON-ready data, plus the columns per element kind."""
    findings = [vars(finding).copy() for finding in report.findings]
    return {
        "rejected": report.rejected,
        "findings": findings,
        "summary": report.counts(),
        **{kind + "s": tally(findings, kind) for kind in KINDS},
    }


def tally(findings, kind="metric"):
    """Fold one report's findings of one kind into the table columns.

    ADDED elements are invented by the converter, so they are counted on their own
    and not in `authored`. A RELABELED element is portable only when it came back
    under another portable tag; under a vendor tag it is vendor-only. A REWRITTEN
    element is portable when the grader proved the normalized trees equal, and
    needs review when it could not or the element came back under another name.
    """
    counts = dict.fromkeys(COLUMNS + ("authored",), 0)
    for finding in findings:
        if finding["kind"] != kind:
            continue
        if finding["outcome"] == "ADDED":
            counts["added"] += 1
            continue
        counts["authored"] += 1
        counts[_column(finding["outcome"], finding.get("detail") or "")] += 1
    return counts


def _column(outcome, detail):
    if outcome == "RELABELED":
        relabeled = _RELABELED.fullmatch(detail)
        if relabeled is None:
            return "other"
        return "portable" if relabeled.group(1) in PORTABLE_DIALECTS else "vendor"
    if outcome == "REWRITTEN":
        return next(
            (column for prefix, column in _REWRITTEN if detail.startswith(prefix)),
            "other",
        )
    return _COLUMN.get(outcome, "other")
