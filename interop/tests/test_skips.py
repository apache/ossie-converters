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

"""Declared capabilities may skip strict checks; runtime failures must not."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from interop.manifest import Manifest, load_manifest
from interop.report import write_report
from interop.runner import Runner, exit_code
from interop.tests.test_foundation import matrix  # noqa: F401 -- subprocess fixture

ROOT = Path(__file__).resolve().parents[2]


def test_declared_preparation_skip_propagates_through_sources_and_cases(matrix):  # noqa: F811
    converter, run, output = matrix
    disabled = replace(converter("beta"), skip="separate ontology matrix")
    runner, report = run([converter("alpha"), disabled])
    results = (
        list(report["preparations"].values()) + report["sources"] + report["cases"]
    )
    skipped = [result for result in results if result["grade"] == "SKIP"]
    assert len(skipped) >= 6
    assert all(result["expected_skip"] for result in skipped)
    assert exit_code(report, strict=True) == 0
    write_report(report, runner.output)
    saved = json.loads((runner.output / "results.json").read_text())
    assert all(
        result["expected_skip"]
        for result in saved["cases"]
        if result["grade"] == "SKIP"
    )


def test_declared_skip_does_not_hide_missing_toolchain(matrix, monkeypatch):  # noqa: F811
    converter, run, _ = matrix
    monkeypatch.setattr("interop.runner.shutil.which", lambda name: None)
    _, report = run(
        [converter("alpha"), replace(converter("beta"), skip="unsupported")]
    )
    assert report["preparations"]["beta"]["expected_skip"]
    assert not report["preparations"]["alpha"].get("expected_skip", False)
    blocked_sources = [
        source for source in report["sources"] if source["owner"] == "alpha"
    ]
    assert blocked_sources and all(
        not source["expected_skip"] for source in blocked_sources
    )
    downstream = [case for case in report["cases"] if case["source"] == "alpha--orders"]
    assert downstream and all(not case["expected_skip"] for case in downstream)
    assert exit_code(report, strict=True) == 1


def test_declared_roundtrip_skip_does_not_hide_export_failure(matrix):  # noqa: F811
    converter, run, _ = matrix
    target = replace(
        converter("beta", export_mode="fail"), roundtrip_skip="compiler unavailable"
    )
    _, report = run([converter("alpha"), target])
    returns = [
        case
        for case in report["cases"]
        if case["tier"] == "B" and case["target"] == "beta"
    ]
    assert returns and all(not case["expected_skip"] for case in returns)
    assert exit_code(report, strict=True) == 1


@pytest.mark.parametrize(
    "section",
    ["preparations", "sources", "cases", "regressions", "document_regressions"],
)
@pytest.mark.parametrize(
    "grade,expected,status",
    [("SKIP", True, 0), ("SKIP", False, 1), ("FAIL", True, 1), ("EMPTY", True, 1)],
)
def test_strict_policy_exempts_only_declared_skips(section, grade, expected, status):
    report = dict(
        preparations={},
        sources=[],
        cases=[],
        regressions=[],
        document_regressions=[],
        harness_errors=[],
    )
    result = dict(grade=grade, expected_skip=expected)
    report[section] = {"target": result} if section == "preparations" else [result]
    assert exit_code(report) == 0
    assert exit_code(report, strict=True) == status
    report["harness_errors"].append("harness failed")
    assert exit_code(report, strict=True) == 2


@pytest.mark.parametrize(
    "capability", ["skip", "import_skip", "roundtrip_skip", "import_requires_files"]
)
def test_document_regression_retains_skip_classification(
    tmp_path, monkeypatch, capability
):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    value = (
        [tmp_path / "missing-schema.json"]
        if capability == "import_requires_files"
        else "declared unsupported"
    )
    target = replace(
        manifest.converters["databricks"], name="fake", **{capability: value}
    )
    runner = Runner(ROOT, Manifest({"fake": target}, {}), tmp_path, prepare=False)
    if capability == "skip":
        runner.report["preparations"]["fake"] = runner.prepare(target)
    else:
        runner.report["preparations"]["fake"] = dict(
            grade="OK", reason="", commands=[], diagnostics=[]
        )
        monkeypatch.setattr(
            runner,
            "acceptance",
            lambda *a, **kw: dict(
                id="accepted",
                grade="OK",
                reason="",
                commands=[],
                diagnostics=[],
                output=str(tmp_path / "conversion/native.yaml"),
                native_gate=dict(grade="SKIP"),
                content=dict(members=1, files=[]),
            ),
        )
    result = runner.document_regression(
        "keys-single", manifest.document_regressions["keys-single"], "fake"
    )
    assert result["grade"] == "SKIP"
    assert result["expected_skip"] == (capability != "import_requires_files")
    runner.report["document_regressions"] = [result]
    assert exit_code(runner.report, strict=True) == (
        1 if capability == "import_requires_files" else 0
    )


@pytest.mark.parametrize("expected", [True, False])
def test_binding_regression_inherits_upstream_skip(tmp_path, expected):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    regression = manifest.regressions["cube-databricks-metric-bindings"]
    runner = Runner(ROOT, manifest, tmp_path, prepare=False)
    runner.report["cases"] = [
        dict(
            id="pair",
            tier="B",
            source="cube--metric-bindings",
            target="databricks",
            grade="SKIP",
            reason="upstream unavailable",
            expected_skip=expected,
        )
    ]
    result = runner.binding_regression("cube-databricks-metric-bindings", regression)
    assert result["grade"] == "SKIP" and result["expected_skip"] == expected
    runner.report["cases"] = []
    runner.report["regressions"] = [result]
    assert exit_code(runner.report, strict=True) == (0 if expected else 1)
