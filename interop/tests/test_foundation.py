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

"""Regression tests for the hub's trust boundaries, not converter implementations."""

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest
import yaml

from interop.checks import ContentError, Validator, compare_inventory, native_content
from interop.execution import diagnostics, run_command
from interop.manifest import Converter, Manifest, ManifestError, expand, load_manifest
from interop.report import write_report
from interop.runner import Runner, exit_code
from interop.__main__ import main


ROOT = Path(__file__).resolve().parents[2]
MODEL = {
    "version": "0.2.0.dev0",
    "name": "orders",
    "datasets": [
        {
            "name": "orders",
            "source": "analytics.public.orders",
            "primary_key": ["id"],
            "fields": [
                {
                    "name": "id",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]
                    },
                }
            ],
        }
    ],
}


def test_committed_manifest_registers_all_converter_projects():
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    assert set(manifest.converters) == {
        p.parent.name
        for p in ROOT.glob("*/pyproject.toml")
        if p.parent.name != "interop"
    } | {"databricks", "polaris", "salesforce"}
    assert (
        sum(a != b for a in manifest.converters for b in manifest.converters) == 19 * 18
    )
    assert manifest.converters["databricks"].toolchain == "maven"
    assert manifest.converters["dbt"].fixtures["orders"].is_file()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(version=2),
        lambda data: data.update(version=True),
        lambda data: data.update(typo=True),
        lambda data: data["converters"]["cube"].update(output="folder"),
        lambda data: data["converters"]["cube"].update(export=["echo", "{typo}"]),
        lambda data: data["converters"]["cube"].update(export="echo {input} {output}"),
        lambda data: data["converters"]["cube"].update(export=["echo", "{input}"]),
        lambda data: data["converters"]["cube"].update(fixtures={}),
        lambda data: data["canonical"].update(tpcds="../outside.yaml"),
    ],
)
def test_manifest_rejects_invalid_contract_before_running(tmp_path, mutation):
    data = yaml.safe_load((ROOT / "interop/manifest.yaml").read_text())
    mutation(data)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ManifestError):
        load_manifest(path, ROOT)


def test_duplicate_manifest_entries_are_rejected(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("version: 1\nversion: 1\n")
    with pytest.raises(ManifestError, match="duplicate"):
        load_manifest(path, ROOT)


def test_arguments_with_spaces_and_shell_syntax_are_literal(tmp_path):
    argv = expand(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "{input}"],
        ROOT,
        tmp_path / "a file; $(false).yaml",
    )
    result = run_command(argv, tmp_path, tmp_path / "logs", 10)
    assert result["returncode"] == 0
    assert Path(result["stdout"]).read_text().strip() == str(
        tmp_path / "a file; $(false).yaml"
    )


def test_timeout_and_missing_executable_have_evidence(tmp_path):
    timeout = run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        tmp_path / "timeout",
        0.1,
    )
    missing = run_command(
        ["ossie-no-such-executable"], tmp_path, tmp_path / "missing", 1
    )
    assert "timed out" in timeout["error"]
    assert missing["error"]
    assert Path(missing["stderr"]).is_file()


def test_child_does_not_inherit_harness_virtual_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/another/environment")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/another/environment")
    result = run_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.getenv('VIRTUAL_ENV')); print(os.getenv('UV_PROJECT_ENVIRONMENT'))",
        ],
        tmp_path,
        tmp_path / "logs",
        10,
    )
    assert Path(result["stdout"]).read_text().splitlines() == ["None", "None"]


@pytest.mark.parametrize(
    "text", ["", "{}", "{source: db.schema.orders, dimensions: [], measures: []}"]
)
def test_empty_native_file_is_never_a_pass(tmp_path, text):
    path = tmp_path / "native.yaml"
    path.write_text(text)
    assert native_content(path, "databricks", "file")["members"] == 0


def test_directory_logs_and_empty_subdirectories_do_not_count(tmp_path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "stdout.txt").write_text("success")
    (tmp_path / "issues.json").write_text('[{"warning": "something"}]')
    assert native_content(tmp_path, "cube", "directory") == {"files": [], "members": 0}
    (tmp_path / "empty" / "cube.yml").write_text(
        "cubes: [{name: orders, dimensions: []}]"
    )
    assert native_content(tmp_path, "cube", "directory")["members"] == 0


def test_malformed_native_output_is_failure_not_empty(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("[")
    with pytest.raises(ContentError):
        native_content(path, "databricks", "file")


def test_validator_checks_schema_and_semantic_constraints(tmp_path):
    model = deepcopy(MODEL)
    model["datasets"][0]["fields"] *= 2
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(model))
    validator = Validator(ROOT)
    _, result = validator.validate(path)
    assert result[
        "errors"
    ]  # Duplicate fields are schema-shaped but semantically invalid.
    path.write_text("version: '0.2.0.dev0'\nsemantic_model: []\n")
    _, result = validator.validate(path)
    assert any("Schema" in error for error in result["errors"])


def test_equal_counts_do_not_hide_removed_fields_or_changed_keys():
    changed = deepcopy(MODEL)
    changed["datasets"][0]["fields"][0]["name"] = "another_id"
    changed["datasets"][0]["primary_key"] = ["another_id"]
    changes = compare_inventory(MODEL, changed)
    assert ["field", "orders", "id"] in changes["removed"]
    assert ["primary_key", "orders", "id"] in changes["removed"]
    assert changes["unchecked"]


def test_structured_diagnostics_and_python_warning_echo(tmp_path):
    path = tmp_path / "stderr.txt"
    path.write_text(
        "[WARNING] ROW_COUNT_METRIC_DROPPED: count was dropped\n"
        "x.py:3: UserWarning: ignored unknown custom_extensions\n"
        "  warnings.warn('ignored unknown custom_extensions')\n"
    )
    found = diagnostics({"stderr": str(path)}, "dbt")
    assert len(found) == 2
    assert found[0]["code"] == "ROW_COUNT_METRIC_DROPPED"
    assert found[1]["foreign_extension"]
    path.write_text(
        "Conversion notices (2):\n  dropped datatype\n  foreign vendor extension dropped\n"
    )
    found = diagnostics({"stderr": str(path)}, "databricks")
    assert len(found) == 2
    assert found[1]["foreign_extension"]


@pytest.fixture
def matrix(tmp_path):
    """Real subprocesses with independently controlled failure modes."""
    fixture = tmp_path / "fixture.yaml"
    fixture.write_text(yaml.safe_dump(MODEL))
    script = tmp_path / "converter.py"
    script.write_text(
        "import json, pathlib, shutil, sys\n"
        f"model = {MODEL!r}\n"
        "mode, inp, out = sys.argv[1:]\n"
        "if mode == 'prepare':\n"
        "    p = pathlib.Path(out); p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
        "elif mode == 'fail':\n"
        "    print('conversion exploded', file=sys.stderr); sys.exit(1)\n"
        "elif mode == 'invalid-return':\n"
        "    pathlib.Path(out).write_text('{}' if pathlib.Path(inp).name.startswith('native') else json.dumps(model))\n"
        "elif mode == 'silent-loss':\n"
        "    if pathlib.Path(inp).name.startswith('native'): model['datasets'][0].pop('primary_key')\n"
        "    pathlib.Path(out).write_text(json.dumps(model))\n"
        "elif mode == 'import':\n"
        "    pathlib.Path(out).write_text(json.dumps(model))\n"
        "elif mode == 'invalid':\n"
        "    pathlib.Path(out).write_text('{}')\n"
        "elif mode == 'empty':\n"
        "    pathlib.Path(out).write_text(json.dumps({'source': 'db.schema.orders'}))\n"
        "else:\n"
        "    pathlib.Path(out).write_text(json.dumps({'source': 'db.schema.orders',\n"
        "        'dimensions': [{'name': 'id', 'expr': 'id'}]}))\n"
    )

    def converter(name, import_mode="import", export_mode="export", toolchain="uv"):
        prefix = [sys.executable, str(script)]
        return Converter(
            name,
            tmp_path,
            toolchain,
            prefix + ["prepare", "unused", str(tmp_path / f"{name}-prepared")],
            prefix + [import_mode, "{input}", "{output}"],
            prefix + [export_mode, "{input}", "{output}"],
            "databricks",
            "file",
            {"orders": fixture},
            None,
        )

    def run(converters):
        output = tmp_path / "results"
        output.mkdir()
        manifest = Manifest(
            {item.name: item for item in converters}, {"orders": fixture}
        )
        runner = Runner(ROOT, manifest, output, timeout=10)
        return runner, runner.run()

    return converter, run, tmp_path


def test_all_six_ordered_pairs_prepare_and_import_sources_once(matrix):
    converter, run, path = matrix
    runner, report = run([converter(name) for name in ("alpha", "beta", "gamma")])
    pairs = [case for case in report["cases"] if case["tier"] == "B"]
    assert len(pairs) == 6
    assert all(case["grade"] == "OK" for case in pairs)
    assert all(
        (path / f"{name}-prepared").read_text() == "x"
        for name in ("alpha", "beta", "gamma")
    )
    assert len([case for case in report["cases"] if case["tier"] == "A"]) == 12
    assert all(
        len(source["commands"]) == 1
        for source in report["sources"]
        if source["owner"] != "canonical"
    )
    write_report(report, runner.output)
    saved = json.loads((runner.output / "results.json").read_text())
    assert saved["cases"][0]["commands"][0]["stdout"].startswith("cases/")
    assert saved["cases"][0]["commands"][0]["argv"][-1].startswith(str(runner.output))
    assert (runner.output / "cases" / pairs[0]["acceptance"] / "tier-B.json").is_file()
    assert "Tier B ordered matrix" in (runner.output / "report.md").read_text()


def test_source_failure_is_attributed_without_aborting_other_sources(matrix):
    converter, run, _ = matrix
    _, report = run([converter("alpha", import_mode="fail"), converter("beta")])
    assert (
        next(source for source in report["sources"] if source["owner"] == "alpha")[
            "grade"
        ]
        == "FAIL"
    )
    alpha_cases = [
        case for case in report["cases"] if case["source"] == "alpha--orders"
    ]
    assert all(case["grade"] == "SKIP" for case in alpha_cases)
    assert any(
        case["grade"] == "OK" and case["source"] == "beta--orders"
        for case in report["cases"]
    )
    assert exit_code(report) == 0
    assert exit_code(report, strict=True) == 1


def test_empty_export_fails_strict_and_blocks_reimport(matrix):
    converter, run, _ = matrix
    _, report = run([converter("alpha"), converter("beta", export_mode="empty")])
    accepted = next(
        case
        for case in report["cases"]
        if case["source"] == "alpha--orders"
        and case["target"] == "beta"
        and case["tier"] == "A"
    )
    returned = next(
        case
        for case in report["cases"]
        if case["source"] == "alpha--orders"
        and case["target"] == "beta"
        and case["tier"] == "B"
    )
    assert accepted["grade"] == "EMPTY"
    assert returned["grade"] == "SKIP"
    assert not returned["commands"]
    assert exit_code(report, strict=True) == 1


def test_missing_toolchain_is_explicit_and_never_satisfies_strict(matrix, monkeypatch):
    converter, run, _ = matrix
    monkeypatch.setattr("interop.runner.shutil.which", lambda executable: None)
    _, report = run([converter("alpha", toolchain="maven"), converter("beta")])
    assert report["preparations"]["alpha"]["grade"] == "SKIP"
    assert "mvn, java" in report["preparations"]["alpha"]["reason"]
    assert all(case["grade"] == "SKIP" for case in report["cases"])
    assert exit_code(report) == 0
    assert exit_code(report, strict=True) == 1


def test_export_failure_does_not_abort_other_targets(matrix):
    converter, run, _ = matrix
    _, report = run([converter("alpha", export_mode="fail"), converter("beta")])
    assert all(
        case["grade"] == "FAIL"
        for case in report["cases"]
        if case["tier"] == "A" and case["target"] == "alpha"
    )
    assert any(
        case["grade"] == "OK" and case["target"] == "beta" for case in report["cases"]
    )


def test_invalid_returned_ossie_is_not_graded_as_success(matrix):
    converter, run, _ = matrix
    _, report = run(
        [converter("alpha"), converter("beta", import_mode="invalid-return")]
    )
    case = next(
        case
        for case in report["cases"]
        if case["tier"] == "B" and case["target"] == "beta"
    )
    assert case["grade"] == "FAIL"
    assert case["validation"]["errors"]


def test_silent_key_loss_is_lossy_even_without_warnings(matrix):
    converter, run, _ = matrix
    _, report = run([converter("alpha"), converter("beta", import_mode="silent-loss")])
    case = next(
        case
        for case in report["cases"]
        if case["tier"] == "B" and case["target"] == "beta"
    )
    assert case["grade"] == "LOSSY"
    assert not case["diagnostics"]
    assert ["primary_key", "orders", "id"] in case["inventory_changes"]["removed"]
    assert (
        exit_code(report, strict=True) == 0
    )  # LOSSY is reported, not an automatic blocker.


def test_preparation_failure_is_not_misclassified_as_missing_toolchain(matrix):
    from dataclasses import replace

    converter, run, path = matrix
    broken = replace(
        converter("alpha"),
        prepare=[sys.executable, "-c", "raise RuntimeError('build failed')"],
    )
    _, report = run([broken, converter("beta")])
    assert report["preparations"]["alpha"]["grade"] == "FAIL"
    assert not (path / "alpha-prepared").exists()
    assert any(
        case["grade"] == "OK" and case["target"] == "beta" for case in report["cases"]
    )


def test_cli_rejects_existing_output_without_overwriting_it(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("keep me")
    assert main(["--output", str(tmp_path)]) == 2
    assert marker.read_text() == "keep me"


def test_cli_persists_harness_errors_in_report(tmp_path, monkeypatch):
    def broken(self):
        raise RuntimeError("harness broke")

    monkeypatch.setattr(Runner, "run", broken)
    output = tmp_path / "results"
    assert main(["--output", str(output)]) == 2
    report = json.loads((output / "results.json").read_text())
    assert "harness broke" in report["harness_errors"][0]


def test_harness_error_is_always_nonzero():
    assert exit_code({"harness_errors": ["unexpected error"]}) == 2


@pytest.mark.parametrize("status, grade", [(0, "OK"), (1, "FAIL"), (2, "SKIP")])
def test_native_gate_status_stays_separate_from_acceptance(
    matrix, monkeypatch, status, grade
):
    from dataclasses import replace

    converter, run, _ = matrix
    runner, report = run([converter("alpha")])
    runner.native = True
    monkeypatch.setenv("OSSIE_INTEROP_TEST_GATE", "enabled")
    target = replace(
        converter("alpha"),
        native_gate={
            "requires_env": "OSSIE_INTEROP_TEST_GATE",
            "command": [sys.executable, "-c", f"raise SystemExit({status})"],
        },
    )
    case = report["cases"][0]
    case["native_gate"] = runner.native_gate(
        target, Path(case["output"]), case["content"], runner.output / "gate"
    )
    assert case["native_gate"]["grade"] == grade
    assert case["grade"] == "OK"
    assert exit_code(report, strict=True) == (1 if status == 1 else 0)


def test_java_version_is_checked_before_maven_build(matrix, monkeypatch):
    converter, run, _ = matrix
    runner, _ = run([converter("alpha")])
    monkeypatch.setattr(
        "interop.runner.shutil.which", lambda executable: sys.executable
    )

    def old_java(result, argv, cwd, evidence, name):
        assert argv == ["java", "-version"]
        return run_command(
            [
                sys.executable,
                "-c",
                "import sys; print('openjdk version \"17.0.1\"', file=sys.stderr)",
            ],
            cwd,
            evidence,
            10,
        )

    monkeypatch.setattr(runner, "command", old_java)
    result = runner.prepare(converter("java", toolchain="maven"))
    assert result["grade"] == "SKIP"
    assert "Java 21+" in result["reason"]
