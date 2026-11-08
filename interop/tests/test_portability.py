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

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
import yaml

from interop.ci_summary import render
from interop.manifest import Manifest, ManifestError, load_manifest
from interop.portability import tally
from interop.report import write_report
from interop.runner import Runner, exit_code

ROOT = Path(__file__).resolve().parents[2]
VARIANT = "M0.ansi.qualified"

# Exports a Metric View-shaped file carrying the input document, and re-imports it
# with one mutation per mode.
HELPER = """
import json, pathlib, sys
action, mode, inp, out = sys.argv[1:]
data = json.loads(json.dumps(__import__('yaml').safe_load(pathlib.Path(inp).read_text())))
if action == 'export':
    if mode == 'refuse-export': sys.exit('cannot express total_amount')
    native = {'source': 'samples.sales.orders',
              'dimensions': [{'name': 'amount', 'expr': 'amount'}], 'ossie': data}
    pathlib.Path(out).write_text(json.dumps(native))
    sys.exit(0)
data = data['ossie']
metric = data['metrics'][0]['expression']['dialects'][0]
if mode == 'refuse-import': sys.exit(1)
if mode == 'garbage':
    pathlib.Path(out).write_text('fields: [unclosed')
    sys.exit(0)
if mode == 'drop-silent': data['metrics'] = []
if mode == 'drop-diagnosed':
    data['metrics'] = []
    print('warning: metric total_amount skipped', file=sys.stderr)
if mode == 'cosmetic': metric['expression'] = 'sum(sales.amount)'
if mode == 'review': metric['expression'] = 'SUM(sales.amount) + 1'
if mode == 'vendor': metric.update(dialect='DATABRICKS', expression='SUM(`amount`)')
pathlib.Path(out).write_text(json.dumps(data))
"""


def fake_runner(tmp_path, mode, *, prepared=True):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    tmp_path.mkdir(parents=True, exist_ok=True)
    helper = tmp_path / "converter.py"
    helper.write_text(HELPER)
    prefix = [sys.executable, str(helper)]
    target = replace(
        manifest.converters["databricks"],
        name="fake",
        cwd=tmp_path,
        export_command=prefix + ["export", mode, "{input}", "{output}"],
        import_command=prefix + ["import", mode, "{input}", "{output}"],
    )
    output = tmp_path / "run"
    output.mkdir()
    runner = Runner(
        ROOT,
        Manifest({"fake": target}, {}, portability=manifest.portability),
        output,
        prepare=False,
    )
    runner.report["preparations"]["fake"] = {
        "grade": "OK" if prepared else "SKIP",
        "reason": "" if prepared else "missing toolchain executable(s): mvn",
        "commands": [],
        "diagnostics": [],
    }
    return runner, target


def run_case(tmp_path, mode, **kwargs):
    runner, target = fake_runner(tmp_path, mode, **kwargs)
    path = runner.manifest.portability.variants[VARIANT]
    return runner, runner.portability_case(VARIANT, path, target)


def metric_outcome(result):
    (finding,) = [f for f in result["grading"]["findings"] if f["kind"] == "metric"]
    return finding["outcome"], finding["detail"]


def test_the_registered_fixtures_are_the_core_ladder():
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    core = yaml.safe_load(
        (ROOT / "ossie/compliance/portability/manifest.yaml").read_text()
    )
    expected = [v["id"] for rung in core["rungs"] for v in rung["variants"]]
    assert list(manifest.portability.variants) == expected
    assert all(path.is_file() for path in manifest.portability.variants.values())
    assert (
        manifest.portability.modes["microsoft-translate-dax"].converter == "microsoft"
    )
    assert "--translate-dax" in (
        manifest.portability.modes["microsoft-translate-dax"].import_command
    )


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("copy", ("PORTABLE", "portable")),
        ("cosmetic", ("REWRITTEN", "portable")),
        ("review", ("REWRITTEN", "review")),
        ("vendor", ("SURVIVED_VENDOR_ONLY", "vendor")),
        ("drop-silent", ("DROPPED_SILENT", "dropped")),
        ("drop-diagnosed", ("DROPPED_DIAGNOSED", "dropped")),
    ],
)
def test_a_round_trip_is_graded_by_the_core_grader(tmp_path, mode, expected):
    _, result = run_case(tmp_path, mode)
    outcome, column = expected
    assert metric_outcome(result)[0] == outcome
    assert result["grading"]["metrics"][column] == 1
    assert result["grading"]["metrics"]["authored"] == 1
    assert result["grading"]["rejected"] is False
    assert len(result["commands"]) == 2


def test_a_dropped_metric_is_diagnosed_only_by_the_converter_output(tmp_path):
    # The same drop with and without a warning naming the metric.
    _, silent = run_case(tmp_path / "a", "drop-silent")
    _, diagnosed = run_case(tmp_path / "b", "drop-diagnosed")
    assert metric_outcome(silent)[0] == "DROPPED_SILENT"
    assert metric_outcome(diagnosed)[0] == "DROPPED_DIAGNOSED"


@pytest.mark.parametrize(
    "mode, grade, commands",
    [("refuse-export", "FAIL", 1), ("refuse-import", "FAIL", 2)],
)
def test_a_refused_document_is_graded_rejected(tmp_path, mode, grade, commands):
    _, result = run_case(tmp_path, mode)
    assert result["grade"] == grade
    assert len(result["commands"]) == commands
    assert result["grading"]["rejected"] is True
    assert result["grading"]["summary"] == {"REJECTED": 2}  # the field and the metric
    assert result["grading"]["metrics"]["rejected"] == 1


def test_an_unreadable_return_is_a_visible_rejection_not_a_harness_error(tmp_path):
    _, result = run_case(tmp_path, "garbage")
    assert result["grade"] == "FAIL"  # the returned document does not validate
    assert result["grading"]["rejected"] is True
    assert "unreadable" in result["grading_note"]


def test_an_unprepared_converter_is_a_visible_skip_and_not_graded(tmp_path):
    _, result = run_case(tmp_path, "copy", prepared=False)
    assert result["grade"] == "SKIP"
    assert "mvn" in result["reason"]
    assert result["grading"] is None
    assert result["commands"] == []


def test_portability_never_changes_the_exit_status(tmp_path):
    runner, result = run_case(tmp_path, "refuse-export")
    runner.report["portability"] = [result]
    assert exit_code(runner.report) == 0
    assert exit_code(runner.report, strict=True) == 0


def test_the_report_carries_the_scores_and_the_evidence(tmp_path):
    runner, result = run_case(tmp_path, "review")
    runner.report["portability"] = [result]
    write_report(runner.report, runner.output)
    text = (runner.output / "report.md").read_text()
    assert "## Portability (report-only)" in text
    # The field came back unchanged, the metric rewritten.
    assert f"| {VARIANT} | fake | OK | PORTABLE=1, REWRITTEN=1 |" in text
    assert "| fake | OK 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |" in text
    saved = json.loads((runner.output / "results.json").read_text())["portability"][0]
    assert saved["grading"]["metrics"]["review"] == 1
    assert not Path(saved["input"]).is_absolute()
    assert (runner.output / saved["input"]).read_text() == (
        runner.manifest.portability.variants[VARIANT].read_text()
    )
    assert (runner.output / "portability" / result["id"] / "result.json").is_file()
    assert "| Portability round trips | 1 | 0 | 0 | 0 | 0 |" in render(
        json.loads((runner.output / "results.json").read_text())
    )


def test_a_summary_of_an_older_run_without_portability_still_renders(tmp_path):
    runner, _ = run_case(tmp_path, "copy")
    report = json.loads(json.dumps(runner.report, default=str))
    del report["portability"]
    assert "| Portability round trips | 0 | 0 | 0 | 0 | 0 |" in render(report)


def test_only_round_trippable_registrations_and_their_modes_are_scheduled():
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    runner = Runner(ROOT, manifest, ROOT / "unused", prepare=False)
    scheduled = {(mode or target.name) for target, mode in runner.portability_targets()}
    for name, converter in manifest.converters.items():
        if converter.skip or converter.roundtrip_skip or converter.import_skip:
            assert name not in scheduled
    assert {"cube", "dbt", "databricks", "microsoft"} <= scheduled
    assert "microsoft-translate-dax" in scheduled
    (mode,) = [t for t, m in runner.portability_targets() if m]
    assert mode.name == "microsoft"  # shares the microsoft preparation
    assert "--translate-dax" in mode.import_command


def test_a_converter_subset_keeps_only_its_modes(tmp_path, monkeypatch):
    from interop.__main__ import main

    seen = {}

    def inspect(self):
        seen["modes"] = set(self.manifest.portability.modes)
        return self.report

    monkeypatch.setattr(Runner, "run", inspect)
    assert main(["--converters", "cube,dbt", "--output", str(tmp_path / "a")]) == 0
    assert seen["modes"] == set()
    assert main(["--converters", "microsoft", "--output", str(tmp_path / "b")]) == 0
    assert seen["modes"] == {"microsoft-translate-dax"}


def _manifest_with(tmp_path, portability):
    data = yaml.safe_load((ROOT / "interop/manifest.yaml").read_text())
    data["portability"] = portability
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize(
    "portability, message",
    [
        (
            {"fixtures": "ossie/compliance/portability/manifest.yaml", "extra": 1},
            "unknown keys",
        ),
        ({"fixtures": "ossie/compliance/portability/missing.yaml"}, "must exist"),
        (
            {
                "fixtures": "ossie/compliance/portability/manifest.yaml",
                "modes": {
                    "x": {"converter": "nope", "import": ["a", "{input}", "{output}"]}
                },
            },
            "registered converter",
        ),
        (
            {
                "fixtures": "ossie/compliance/portability/manifest.yaml",
                "modes": {
                    "x": {
                        "converter": "bigquery",
                        "import": ["a", "{input}", "{output}"],
                    }
                },
            },
            "registered converter",
        ),
        (
            {
                "fixtures": "ossie/compliance/portability/manifest.yaml",
                "modes": {
                    "cube": {
                        "converter": "cube",
                        "import": ["a", "{input}", "{output}"],
                    }
                },
            },
            "shadows",
        ),
        (
            {
                "fixtures": "ossie/compliance/portability/manifest.yaml",
                "modes": {"x": {"converter": "cube", "import": ["a", "{input}"]}},
            },
            "placeholders",
        ),
    ],
)
def test_portability_configuration_is_validated(tmp_path, portability, message):
    with pytest.raises(ManifestError, match=message):
        load_manifest(_manifest_with(tmp_path, portability), ROOT)


@pytest.mark.parametrize(
    "variant, message",
    [
        (
            {"id": "M0/escape", "file": "m0/m0_sum_bare.ossie.yaml"},
            "invalid variant id",
        ),
        ({"id": "M0.sum.bare", "file": "m0/m0_sum_bare.ossie.yaml"}, "duplicate"),
        ({"id": "M9.x", "file": "m9/none.ossie.yaml"}, "must be a file under"),
        # Inside the repository, but outside the fixtures directory.
        (
            {"id": "M9.y", "file": "../../../interop/manifest.yaml"},
            "must be a file under",
        ),
    ],
)
def test_core_fixture_entries_are_validated(tmp_path, monkeypatch, variant, message):
    core = yaml.safe_load(
        (ROOT / "ossie/compliance/portability/manifest.yaml").read_text()
    )
    core["rungs"][0]["variants"].append(variant)
    copy = ROOT / "ossie/compliance/portability/.interop-test-manifest.yaml"
    copy.write_text(yaml.safe_dump(core))
    try:
        path = _manifest_with(
            tmp_path,
            {"fixtures": "ossie/compliance/portability/.interop-test-manifest.yaml"},
        )
        with pytest.raises(ManifestError, match=message):
            load_manifest(path, ROOT)
    finally:
        copy.unlink()


def test_the_metric_columns():
    findings = [
        {"kind": "metric", "outcome": "PORTABLE"},
        {"kind": "metric", "outcome": "RELABELED"},
        {
            "kind": "metric",
            "outcome": "REWRITTEN",
            "detail": "cosmetic (normalized trees equal)",
        },
        {
            "kind": "metric",
            "outcome": "REWRITTEN",
            "detail": "review: could not compare trees",
        },
        {"kind": "metric", "outcome": "PLACEHOLDER"},
        {"kind": "metric", "outcome": "ADDED"},
        {"kind": "metric", "outcome": "SOMETHING_NEW"},
        {"kind": "field", "outcome": "DROPPED_SILENT"},
    ]
    counts = tally(findings)
    assert counts["portable"] == 3
    assert counts["review"] == 1
    assert counts["placeholder"] == 1
    assert counts["added"] == 1
    assert counts["other"] == 1  # a newer grader's outcome stays visible
    assert counts["dropped"] == 0  # fields are not metrics
    assert counts["authored"] == 6
