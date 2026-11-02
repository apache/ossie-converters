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

"""Independent binding oracles and mutations reproducing wrong-cube attribution."""

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest
import yaml

from interop.bindings import check_bindings
from interop.checks import native_content
from interop.manifest import Manifest, ManifestError, load_manifest
from interop.report import write_report
from interop.runner import Runner, exit_code


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "interop/fixtures/cube/metric_bindings/model.yml"
ORACLE = ROOT / "interop/fixtures/bindings/sales.yaml"


def expectations():
    return yaml.safe_load(ORACLE.read_text())["metrics"]


def model():
    return yaml.safe_load(FIXTURE.read_text())


def write_model(path, data):
    path.write_text(yaml.safe_dump(data))
    return path


def test_original_fixtures_satisfy_independently_authored_oracles():
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    assert len(manifest.regressions) == 2
    for regression in manifest.regressions.values():
        result = check_bindings(
            manifest.converters[regression.source].fixtures[regression.fixture],
            regression.metrics,
        )
        assert result["grade"] == "OK", result


def test_wrong_cube_is_detected_even_when_column_exists_on_both_tables(tmp_path):
    original = model()
    changed = deepcopy(original)
    sales, customer = changed["cubes"]
    customer["dimensions"].append(
        {"name": "sales_amount", "sql": "sales_amount", "type": "number"}
    )
    customer["measures"].append(sales["measures"].pop(0))
    path = write_model(tmp_path / "changed.yaml", changed)
    assert native_content(path, "cube", "file")["members"] > 0
    result = check_bindings(path, expectations())
    metric = result["metrics"]["total_revenue"]
    assert metric["grade"] == "FAIL"
    assert metric["actual"] == [
        {
            "function": "sum",
            "source": "analytics.public.customer",
            "column": "sales_amount",
            "grain": "analytics.public.customer",
        }
    ]
    assert sum(len(cube["measures"]) for cube in changed["cubes"]) == sum(
        len(cube["measures"]) for cube in original["cubes"]
    )


def test_correct_column_on_wrong_aggregation_grain_also_fails(tmp_path):
    data = model()
    sales, customer = data["cubes"]
    measure = sales["measures"].pop(0)
    measure["sql"] = "{sales.sales_amount}"
    customer["measures"].append(measure)
    result = check_bindings(
        write_model(tmp_path / "wrong_grain.yaml", data), expectations()
    )
    actual = result["metrics"]["total_revenue"]["actual"][0]
    assert actual["source"] == "analytics.public.sales"
    assert actual["grain"] == "analytics.public.customer"
    assert result["grade"] == "FAIL"


def test_tpcds_decomposed_sales_aggregate_cannot_move_to_customer(tmp_path):
    root = ROOT / "cube/tests/fixtures/tpcds_cube"
    cubes = [
        cube
        for path in sorted(root.rglob("*.yml"))
        for cube in yaml.safe_load(path.read_text()).get("cubes", [])
    ]
    sales = next(c for c in cubes if c["name"] == "store_sales")
    customer = next(c for c in cubes if c["name"] == "customer")
    part = next(
        m for m in sales["measures"] if m["name"] == "customer_lifetime_value_part_1"
    )
    sales["measures"].remove(part)
    customer["measures"].append(part)
    public = next(
        m for m in sales["measures"] if m["name"] == "customer_lifetime_value"
    )
    public["sql"] = public["sql"].replace(
        "{CUBE.customer_lifetime_value_part_1}",
        "{customer.customer_lifetime_value_part_1}",
    )
    expected = yaml.safe_load(
        (ROOT / "interop/fixtures/bindings/tpcds.yaml").read_text()
    )["metrics"]
    result = check_bindings(
        write_model(tmp_path / "wrong_cube.yaml", {"cubes": cubes}), expected
    )
    assert result["metrics"]["total_sales"]["grade"] == "OK"
    metric = result["metrics"]["customer_lifetime_value"]
    assert metric["grade"] == "FAIL"
    assert any(
        binding["function"] == "sum" and binding["source"] == "tpcds.public.customer"
        for binding in metric["actual"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["cubes"][0]["measures"].pop(0),
        lambda data: data["cubes"][0]["measures"][0].update(type="max"),
        lambda data: data["cubes"][0].update(sql_table="analytics.public.other_sales"),
        lambda data: data["cubes"][1]["dimensions"][0].update(
            sql="another_customer_id"
        ),
    ],
)
def test_missing_metric_function_table_or_count_key_changes_fail(tmp_path, mutation):
    data = model()
    mutation(data)
    assert (
        check_bindings(write_model(tmp_path / "changed.yaml", data), expectations())[
            "grade"
        ]
        == "FAIL"
    )


def test_dimension_alias_resolves_to_physical_column_not_member_name(tmp_path):
    data = model()
    sales = data["cubes"][0]
    sales["dimensions"][2]["sql"] = "gross_amount"
    sales["measures"][0]["sql"] = "{CUBE.sales_amount}"
    expected = expectations()
    for entries in expected.values():
        for binding in entries:
            if binding["source"] == "analytics.public.sales":
                binding["column"] = "gross_amount"
    assert (
        check_bindings(write_model(tmp_path / "aliases.yaml", data), expected)["grade"]
        == "OK"
    )


def test_same_cube_member_shorthand_is_resolved(tmp_path):
    data = model()
    for measure in data["cubes"][0]["measures"][1:]:
        measure["sql"] = measure["sql"].replace(
            "{CUBE.total_revenue}", "{total_revenue}"
        )
    assert (
        check_bindings(write_model(tmp_path / "shorthand.yaml", data), expectations())[
            "grade"
        ]
        == "OK"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "{CUBE.total_revenue}",  # recursive measure
        "{CUBE.missing}",
        "SUM(sales_amount) OVER ()",  # window semantics outside inspector scope
        "(SELECT SUM(sales_amount) FROM other_table)",
        "UNKNOWN_UDF(sales_amount)",
        "SUM(sales_amount); SELECT credit_limit",
    ],
)
def test_unresolvable_or_unsupported_sql_fails_closed(tmp_path, sql):
    data = model()
    data["cubes"][0]["measures"][0].update(type="number", sql=sql)
    result = check_bindings(
        write_model(tmp_path / "unsupported.yaml", data), expectations()
    )
    assert result["metrics"]["total_revenue"]["grade"] == "FAIL"
    assert result["metrics"]["total_revenue"]["error"]


def test_template_text_in_literals_and_comments_is_not_a_binding(tmp_path):
    data = model()
    data["cubes"][0]["measures"][0]["sql"] = (
        "COALESCE({CUBE.sales_amount}, 0) + CASE WHEN 'it''s {missing.column}' = 'x' "
        "THEN 0 ELSE 0 END /* {other.missing} */ -- {missing.again}\n"
    )
    result = check_bindings(
        write_model(tmp_path / "literals.yaml", data), expectations()
    )
    assert result["grade"] == "OK", result


def test_duplicate_native_cubes_and_yaml_keys_are_failures(tmp_path):
    data = model()
    data["cubes"].append(deepcopy(data["cubes"][0]))
    assert (
        check_bindings(write_model(tmp_path / "duplicate.yaml", data), expectations())[
            "grade"
        ]
        == "FAIL"
    )
    path = tmp_path / "duplicate_key.yaml"
    path.write_text("cubes: []\ncubes: []\n")
    assert check_bindings(path, expectations())["grade"] == "FAIL"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["regressions"]["cube-databricks-tpcds"].update(
            fixture="missing"
        ),
        lambda data: data["regressions"]["cube-databricks-tpcds"].update(via="cube"),
        lambda data: data["regressions"]["cube-databricks-tpcds"].update(
            source="databricks"
        ),
        lambda data: data["regressions"]["cube-databricks-tpcds"].update(typo=True),
    ],
)
def test_bad_regression_configuration_fails_before_execution(tmp_path, mutation):
    data = yaml.safe_load((ROOT / "interop/manifest.yaml").read_text())
    mutation(data)
    path = write_model(tmp_path / "manifest.yaml", data)
    with pytest.raises(ManifestError):
        load_manifest(path, ROOT)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda oracle: oracle.update(version=True),
        lambda oracle: oracle.update(metrics={}),
        lambda oracle: oracle["metrics"]["total_revenue"][0].pop("grain"),
        lambda oracle: oracle["metrics"]["total_revenue"][0].update(column=""),
        lambda oracle: oracle["metrics"]["total_revenue"][0].update(function="custom"),
        lambda oracle: oracle["metrics"].update(total_revenue="not a binding list"),
    ],
)
def test_invalid_oracle_is_rejected_as_configuration_error(tmp_path, mutation):
    oracle = {"version": 1, "metrics": expectations()}
    mutation(oracle)
    write_model(tmp_path / "oracle.yaml", oracle)
    write_model(tmp_path / "native.yaml", model())
    command = ["fake", "{input}", "{output}"]
    converters = {
        name: {
            "cwd": ".",
            "toolchain": "uv",
            "prepare": ["fake"],
            "import": command,
            "export": command,
            "native_format": name,
            "output": "directory" if name == "cube" else "file",
            "fixtures": {"binding": "native.yaml"},
        }
        for name in ("cube", "databricks")
    }
    path = write_model(
        tmp_path / "manifest.yaml",
        {
            "version": 1,
            "canonical": {"example": "native.yaml"},
            "converters": converters,
            "regressions": {
                "binding": {
                    "source": "cube",
                    "via": "databricks",
                    "fixture": "binding",
                    "expectations": "oracle.yaml",
                }
            },
        },
    )
    with pytest.raises(ManifestError):
        load_manifest(path, tmp_path)


def test_converter_selection_excludes_regressions_requiring_an_unselected_converter(
    tmp_path, monkeypatch
):
    from interop.__main__ import main

    def inspect(self):
        assert set(self.manifest.converters) == {"cube", "dbt"}
        assert not self.manifest.regressions
        return self.report

    monkeypatch.setattr(Runner, "run", inspect)
    assert (
        main(["--converters", "cube,dbt", "--output", str(tmp_path / "results")]) == 0
    )


def test_binding_failure_blocks_strict_mode_but_is_report_only_by_default():
    report = {
        "harness_errors": [],
        "preparations": {},
        "sources": [],
        "cases": [],
        "regressions": [{"grade": "FAIL"}],
    }
    assert exit_code(report) == 0
    assert exit_code(report, strict=True) == 1


@pytest.mark.parametrize(
    "mode, grade",
    [
        ("valid", "OK"),
        ("wrong-cube", "FAIL"),
        ("exit", "FAIL"),
        ("empty", "EMPTY"),
        ("blocked", "SKIP"),
    ],
)
def test_runner_persists_binding_failures_and_respects_dependency_results(
    tmp_path, mode, grade
):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    name = "cube-databricks-metric-bindings"
    regression = manifest.regressions[name]
    data = model()
    if mode == "wrong-cube":
        sales, customer = data["cubes"]
        customer["measures"].append(sales["measures"].pop(0))
    native = write_model(tmp_path / "native.yaml", data)
    helper = tmp_path / "export.py"
    helper.write_text(
        "import pathlib, shutil, sys\n"
        "mode, fixture, out = sys.argv[1:]\n"
        "if mode == 'exit': sys.exit(1)\n"
        "pathlib.Path(out).mkdir()\n"
        "if mode != 'empty': shutil.copyfile(fixture, pathlib.Path(out) / 'model.yaml')\n"
    )
    cube = replace(
        manifest.converters["cube"],
        cwd=tmp_path,
        export_command=[sys.executable, str(helper), mode, str(native), "{output}"],
    )
    selected = Manifest({"cube": cube}, {}, {name: regression})
    runner = Runner(ROOT, selected, tmp_path, prepare=False)
    runner.report["cases"] = [
        {
            "id": "pair",
            "tier": "B",
            "source": "cube--metric-bindings",
            "target": "databricks",
            "grade": "SKIP" if mode == "blocked" else "LOSSY",
            "reason": "upstream failed",
            "output": str(tmp_path / "returned.yaml"),
        }
    ]
    result = runner.binding_regression(name, regression)
    assert result["grade"] == grade, result
    if mode == "blocked":
        assert not result["commands"]
        assert "pairwise return unavailable" in result["reason"]
    if mode == "wrong-cube":
        assert (
            result["returned_bindings"]["metrics"]["total_revenue"]["grade"] == "FAIL"
        )
    runner.report["cases"] = []  # persist the independently graded regression
    runner.report["regressions"] = [result]
    write_report(runner.report, tmp_path)
    saved = json.loads((tmp_path / "results.json").read_text())["regressions"][0]
    assert saved["grade"] == grade
    assert saved["expectations"] == f"regressions/{name}/expectations.yaml"
    assert (tmp_path / f"regressions/{name}/result.json").is_file()
    assert "Native metric-binding regressions" in (tmp_path / "report.md").read_text()
    assert exit_code(runner.report) == 0
    assert exit_code(runner.report, strict=True) == (0 if grade == "OK" else 1)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("OSSIE_INTEROP_INTEGRATION") != "1",
    reason="set OSSIE_INTEROP_INTEGRATION=1 with prepared Cube/Databricks and Java 21+",
)
@pytest.mark.parametrize(
    "name", ["cube-databricks-tpcds", "cube-databricks-metric-bindings"]
)
def test_real_cube_databricks_cube_preserves_native_aggregate_bindings(tmp_path, name):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    regression = manifest.regressions[name]
    cube = manifest.converters["cube"]
    selected = Manifest(
        {
            "cube": replace(
                cube, fixtures={regression.fixture: cube.fixtures[regression.fixture]}
            ),
            "databricks": replace(manifest.converters["databricks"], fixtures={}),
        },
        {},
        {name: regression},
    )
    runner = Runner(ROOT, selected, tmp_path, prepare=False)
    report = runner.run()
    write_report(report, tmp_path)
    result = report["regressions"][0]
    assert result["grade"] == "OK", json.dumps(result, indent=2)
    assert result["source_bindings"]["grade"] == "OK"
    assert result["returned_bindings"]["grade"] == "OK"
    assert len(result["returned_bindings"]["metrics"]) == len(regression.metrics)
    assert result["commands"]  # includes the final source-native export
