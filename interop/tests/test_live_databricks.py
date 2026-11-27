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

import copy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp
import yaml

from interop.live_databricks import (
    DEFAULT_SOURCE,
    DATASETS,
    GRAINS,
    MEASURES,
    ROW_COUNT,
    LiveCheckError,
    bind_query,
    compare,
    comparison_sql,
    cube_requests,
    deployment_plan,
    execute,
    prepare_model,
    record_comparison,
    run_check,
)
from interop.manifest import Manifest, load_manifest
from interop.report import write_report
from interop.runner import Runner, exit_code

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def model():
    fields = [{"name": "id", "expression": {"sql": "id"}}]
    return {
        "version": "0.3.0.dev0",
        "name": "tpcds_retail_model",
        "datasets": [
            {
                "name": name,
                "source": "tpcds.public." + name,
                "fields": copy.deepcopy(fields),
                "metrics": [
                    {"name": m, "expression": {"sql": "SUM(id)"}} for m in MEASURES[:3]
                ]
                if name == "store_sales"
                else [],
            }
            for name in sorted(DATASETS)
        ],
        "metrics": [
            {"name": m, "expression": {"sql": "SUM(store_sales.id)"}}
            for m in MEASURES[3:]
        ],
        "relationships": [],
    }


@pytest.fixture
def prepared(model):
    return prepare_model(yaml.safe_dump(model), DEFAULT_SOURCE)


def write_deployment(model, directory, scratch="catalog.scratch"):
    """Fake CLI files for fault injection; a separate integration test uses the CLI."""
    (directory / "metric_views").mkdir(parents=True)
    deployed = copy.deepcopy(model)
    for original, dataset in zip(model["datasets"], deployed["datasets"]):
        view = {
            "version": "1.1",
            "source": original["source"],
            "dimensions": [
                {"name": f["name"], "expr": f["name"]} for f in original["fields"]
            ],
            "measures": [
                {"name": m["name"], "expr": "COUNT(*)"} for m in original["metrics"]
            ],
        }
        (directory / "metric_views" / f"{dataset['name']}.yaml").write_text(
            yaml.safe_dump(view)
        )
        dataset["source"] = f"{scratch}.{dataset['name']}"
        for metric in dataset["metrics"]:
            metric["expression"]["dialects"] = [
                {"dialect": "DATABRICKS", "expression": f"MEASURE({metric['name']})"}
            ]
    path = directory / f"{model['name']}.ossie.yaml"
    path.write_text(yaml.safe_dump(deployed))
    return path


@pytest.fixture
def assets(prepared, tmp_path):
    write_deployment(prepared, tmp_path)
    return deployment_plan(prepared, tmp_path, "catalog.scratch", "testtoken")


def test_current_model_preparation_preserves_semantics_and_adapts_sample(model):
    original = copy.deepcopy(model)
    sample = prepare_model(yaml.safe_dump(model), DEFAULT_SOURCE)
    custom = prepare_model(yaml.safe_dump(model), "other.fixture")
    assert model == original
    assert sample["metrics"] == original["metrics"]
    assert sample["relationships"] == original["relationships"]
    for dataset in sample["datasets"]:
        assert dataset["metrics"][-1] == {
            "name": ROW_COUNT,
            "expression": {"sql": "COUNT(*)"},
        }
        if dataset["name"] == "date_dim":
            assert "date_format(d_date, 'MMMM') AS d_month_name" in dataset["source"]
        else:
            assert dataset["source"] == DEFAULT_SOURCE + "." + dataset["name"]
    assert all(d["source"] == "other.fixture." + d["name"] for d in custom["datasets"])


@pytest.mark.parametrize("mutation", ["legacy", "source", "metric", "collision"])
def test_preparation_rejects_legacy_or_unexpected_export(model, mutation):
    if mutation == "legacy":
        model = {"source": "tpcds.public.store_sales", "measures": []}
    elif mutation == "source":
        model["datasets"][0]["source"] = "unrelated.schema.table"
    elif mutation == "metric":
        model["metrics"].pop()
    else:
        model["datasets"][0]["fields"].append({"name": ROW_COUNT})
    with pytest.raises(LiveCheckError):
        prepare_model(yaml.safe_dump(model), DEFAULT_SOURCE)


def test_deployment_binds_all_datasets_to_unique_run_assets(prepared, assets, tmp_path):
    assert {a["dataset"] for a in assets} == DATASETS
    assert all(
        a["target"] == "catalog.scratch.ossie_tpcds_testtoken_" + a["dataset"]
        for a in assets
    )
    assert all(f"MEASURE(`{ROW_COUNT}`)" in a["projection"] for a in assets)
    deployed = yaml.safe_load((tmp_path / "live-bindings.ossie.yaml").read_text())
    assert deployed["metrics"] == prepared["metrics"]
    assert deployed["relationships"] == prepared["relationships"]
    assert {d["source"] for d in deployed["datasets"]} == {a["target"] for a in assets}
    # A second concurrent run cannot overwrite this run's targets.
    second = deployment_plan(prepared, tmp_path, "catalog.scratch", "othertoken")
    assert not {a["target"] for a in second} & {a["target"] for a in assets}


@pytest.mark.parametrize(
    "mutation",
    [
        "global",
        "relationship",
        "source",
        "field",
        "measure",
        "binding",
        "delimiter",
        "missing",
        "rewritten_metric",
        "keys",
    ],
)
def test_deployment_artifact_regressions_are_rejected(prepared, tmp_path, mutation):
    path = write_deployment(prepared, tmp_path)
    deployed = yaml.safe_load(path.read_text())
    asset_path = tmp_path / "metric_views/customer.yaml"
    view = yaml.safe_load(asset_path.read_text())
    if mutation == "global":
        deployed["metrics"][0]["expression"] = {"sql": "0"}
    elif mutation == "relationship":
        deployed["relationships"] = [{"name": "changed"}]
    elif mutation == "source":
        deployed["datasets"][0]["source"] = "catalog.wrong.customer"
    elif mutation == "field":
        view["dimensions"] = []
    elif mutation == "measure":
        view["measures"] = []
    elif mutation == "binding":
        deployed["datasets"][0]["metrics"][0]["expression"] = {"sql": "COUNT(*)"}
    elif mutation == "delimiter":
        view["comment"] = "$$"
    elif mutation == "rewritten_metric":
        deployed["datasets"][0]["metrics"] = []
    elif mutation == "keys":
        deployed["datasets"][0]["primary_key"] = ["id"]
    elif mutation == "missing":
        asset_path.unlink()
    path.write_text(yaml.safe_dump(deployed))
    if mutation != "missing":
        asset_path.write_text(yaml.safe_dump(view))
    with pytest.raises((LiveCheckError, OSError)):
        deployment_plan(prepared, tmp_path, "catalog.scratch", "test")


def test_query_binding_replaces_tables_without_rewriting_literals_or_aliases(assets):
    sql = "SELECT s.id, 'tpcds.public.store_sales' AS literal FROM tpcds.public.store_sales AS s LEFT JOIN tpcds.public.customer AS c ON s.id = c.id"
    baseline = bind_query(sql, assets, deployed=False)
    candidate = bind_query(sql, assets, deployed=True)
    assert (
        "'tpcds.public.store_sales'" in baseline
        and "'tpcds.public.store_sales'" in candidate
    )
    assert "samples.tpcds_sf1.store_sales AS s" in baseline
    assert "catalog.scratch.ossie_tpcds_testtoken_store_sales" in candidate
    assert "catalog.scratch.ossie_tpcds_testtoken_customer" in candidate
    tree = sqlglot.parse_one(candidate, read="databricks")
    assert {q.alias for q in tree.find_all(exp.Subquery)} >= {"s", "c"}
    assert "s.id = c.id" in candidate
    assert "samples.tpcds_sf1" not in candidate
    with pytest.raises(LiveCheckError):
        bind_query("SELECT 1", assets, deployed=True)
    with pytest.raises(LiveCheckError):
        bind_query("SELECT * FROM tpcds.public.unexpected", assets, deployed=True)


def test_requests_cover_original_model_metrics_and_native_local_metrics(prepared):
    requests = cube_requests(prepared)
    assert set(requests) == {*GRAINS, "dataset:store_sales"}
    assert requests["dataset:store_sales"]["measures"] == [
        f"store_sales.{m}" for m in MEASURES[:3]
    ]
    assert requests["total"]["dimensions"] == []
    for grain, field in GRAINS.items():
        assert requests[grain]["measures"] == [
            f"tpcds_retail_model.{m}" for m in MEASURES
        ]
        query = comparison_sql("SELECT 1", "SELECT 2", field)
        assert "FULL OUTER JOIN" in query
        assert all(f"AS `{m}`" in query for m in MEASURES)
        assert "GREATEST(1E-8" in query
        if field:
            assert (
                f"c.`tpcds_retail_model__{field}` <=> m.`tpcds_retail_model__{field}`"
                in query
            )


def response(data=None, state="SUCCEEDED"):
    return SimpleNamespace(
        status=SimpleNamespace(state=SimpleNamespace(value=state)),
        result=SimpleNamespace(data_array=data),
        statement_id="test",
    )


def result_record():
    return {
        "grade": "OK",
        "reason": "",
        "grains": {},
        "dataset_rows": {},
        "dataset_measures": {},
    }


@pytest.mark.parametrize(
    "outcome",
    [
        "match",
        "mismatch",
        "missing",
        "empty",
        "error",
        "duplicates",
        "shape",
        "create",
        "cleanup",
        "transport",
        "native_mismatch",
        "first_create",
    ],
)
def test_live_lifecycle_checks_all_assets_and_cleans_up_only_created_views(
    assets, outcome
):
    calls = []
    creates = []

    def execute_statement(*, warehouse_id, statement, wait_timeout):
        assert warehouse_id == "test-warehouse"
        assert wait_timeout == "0s"
        calls.append(statement)
        if statement.startswith("CREATE"):
            if outcome == "first_create" or (outcome == "create" and len(creates) == 2):
                return response(state="FAILED")
            creates.append(statement.split()[2])
        elif statement.startswith("DROP") and outcome == "cleanup":
            return response(state="FAILED")
        elif statement.startswith("SELECT COUNT(*)"):
            return response([["10", "1" if outcome == "duplicates" else "0"]])
        elif statement.startswith("WITH c"):
            if outcome == "error":
                return response(state="FAILED")
            if outcome == "transport":
                raise RuntimeError("private connection information")
            width = 5 if "tpcds_retail_model__total_sales" in statement else 3
            data = [["1", "1", "0", *(["0"] * width)]]
            if outcome == "mismatch" or (outcome == "native_mismatch" and width == 3):
                data[0][-1] = "1"
            elif outcome == "missing":
                data[0][2] = "1"
            elif outcome == "empty":
                data[0][:2] = ["0", "0"]
            elif outcome == "shape":
                data = [["1"]]
            return response(data)
        return response()

    result = result_record()
    queries = {
        key: "SELECT * FROM tpcds.public.store_sales"
        for key in [*GRAINS, "dataset:store_sales"]
    }
    api = SimpleNamespace(execute_statement=execute_statement)
    if outcome == "transport":
        with pytest.raises(RuntimeError):
            compare(api, "test-warehouse", assets, queries, 30, result)
    else:
        compare(api, "test-warehouse", assets, queries, 30, result)
        assert result["grade"] == ("OK" if outcome == "match" else "FAIL")
    drops = [
        q.removeprefix("DROP VIEW IF EXISTS ") for q in calls if q.startswith("DROP")
    ]
    assert drops == list(reversed(creates))
    assert len(creates) == (
        0 if outcome == "first_create" else 2 if outcome == "create" else 5
    )
    if outcome == "match":
        assert set(result["grains"]) == set(GRAINS)
        assert set(result["dataset_rows"]) == DATASETS
        assert set(
            result["dataset_measures"]["store_sales"]["measure_mismatches"]
        ) == set(MEASURES[:3])
    if outcome == "cleanup":
        assert "5 run-owned" in result["cleanup_error"]


def test_statement_timeout_cancels_without_exposing_sdk_details(monkeypatch):
    times = iter([0, 2, 3])
    monkeypatch.setattr("interop.live_databricks.time.monotonic", lambda: next(times))
    cancelled = []
    api = SimpleNamespace(
        execute_statement=lambda **kw: response(state="RUNNING"),
        cancel_execution=cancelled.append,
    )
    with pytest.raises(LiveCheckError, match="timed out"):
        execute(api, "warehouse", "SELECT 1", 1)
    assert cancelled == ["test"]
    api = SimpleNamespace(execute_statement=lambda **kw: response(state="FAILED"))
    with pytest.raises(LiveCheckError, match="statement FAILED"):
        execute(api, "warehouse", "SELECT 1", 1)


def test_report_includes_dataset_and_model_validation(tmp_path):
    report = Runner(ROOT, Manifest({}, {}), tmp_path).run()
    live = result_record()
    summary = record_comparison([[1, 1, 0, 0]], ["total_sales"])
    live["grains"]["total"] = summary
    live["dataset_rows"]["store_sales"] = 10
    live["dataset_measures"]["store_sales"] = summary
    report["live_databricks"] = live
    write_report(report, tmp_path)
    text = (tmp_path / "report.md").read_text()
    assert "Native dataset measures" in text
    assert "Model queries over deployed dataset projections" in text
    assert "store_sales" in text


def test_matrix_requires_live_check_only_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.delenv("OSSIE_DATABRICKS_WAREHOUSE_ID", raising=False)
    default = Runner(ROOT, Manifest({}, {}), tmp_path).run()
    assert "live_databricks" not in default
    assert exit_code(default) == 0
    opted_in = Runner(ROOT, Manifest({}, {}), tmp_path, live_databricks=True).run()
    assert "OSSIE_DATABRICKS_WAREHOUSE_ID" in opted_in["live_databricks"]["reason"]
    assert exit_code(opted_in) == 1


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("OSSIE_INTEROP_INTEGRATION") != "1",
    reason="set OSSIE_INTEROP_INTEGRATION=1 with prepared Cube and Databricks converters",
)
def test_real_cube_import_and_current_databricks_deploy_without_warehouse(tmp_path):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    input_path = tmp_path / "cube.ossie.yaml"
    environment = {
        k: v
        for k, v in os.environ.items()
        if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }
    imported = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "ossie-cube",
            "import",
            "-i",
            str(manifest.converters["cube"].fixtures["tpcds"]),
            "-o",
            str(input_path),
        ],
        cwd=ROOT / "cube",
        capture_output=True,
        text=True,
        env=environment,
    )
    assert imported.returncode == 0, imported.stderr
    prepared = prepare_model(input_path.read_text(), DEFAULT_SOURCE)
    adapted = tmp_path / "input.ossie.yaml"
    adapted.write_text(yaml.safe_dump(prepared))
    deployed = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "ossie-databricks",
            "deploy",
            str(adapted),
            "--catalog",
            "catalog",
            "--schema",
            "scratch",
            "-o",
            str(tmp_path / "deploy"),
        ],
        cwd=ROOT / "databricks",
        capture_output=True,
        text=True,
        env=environment,
    )
    assert deployed.returncode == 0, deployed.stderr
    assets = deployment_plan(
        prepared, tmp_path / "deploy", "catalog.scratch", "regression"
    )
    assert len(assets) == 5
    fact = next(a for a in assets if a["dataset"] == "store_sales")
    assert fact["metrics"] == list(MEASURES[:3])
    assert "customer_lifetime_value" not in {
        m["name"] for m in yaml.safe_load(fact["definition"])["measures"]
    }
    assert "MEASURE(`ossie_parity_row_count`)" in fact["projection"]
    if os.environ.get("OSSIE_CUBE_REPO"):
        requests = tmp_path / "requests.json"
        requests.write_text(json.dumps(cube_requests(prepared)))
        fixture = manifest.converters["cube"].fixtures["tpcds"]
        files = sorted([*fixture.rglob("*.yml"), *fixture.rglob("*.yaml")])
        compiled = subprocess.run(
            [
                "node",
                str(ROOT / "cube/tools/cube_compile.js"),
                "--databricks-queries",
                str(requests),
                *map(str, files),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        queries = json.loads(compiled.stdout)
        assert set(queries) == set(cube_requests(prepared))
        for sql, params in queries.values():
            assert not params
            reference = bind_query(sql, assets, deployed=False)
            candidate = bind_query(sql, assets, deployed=True)
            assert "tpcds.public." not in reference
            assert "tpcds.public." not in candidate
            assert "catalog.scratch.ossie_tpcds_regression_" in candidate
            sqlglot.parse_one(
                comparison_sql(reference, candidate, None), read="databricks"
            )


@pytest.mark.parametrize("failure", [None, "deploy", "compile", "params", "export"])
def test_run_check_uses_current_deploy_before_any_remote_calls(
    model, tmp_path, monkeypatch, failure
):
    import sys

    monkeypatch.setenv("OSSIE_DATABRICKS_WAREHOUSE_ID", "warehouse")
    monkeypatch.setenv("OSSIE_DATABRICKS_SCRATCH_SCHEMA", "catalog.scratch")
    monkeypatch.delenv("OSSIE_DATABRICKS_TPCDS_SCHEMA", raising=False)
    exported = tmp_path / "export.ossie.yaml"
    exported.write_text(
        yaml.safe_dump(model if failure != "export" else {"source": "legacy.view"})
    )
    output = tmp_path / "results"
    output.mkdir()
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    calls = []
    remote = []

    def command(result, argv, cwd, evidence, converter):
        calls.append((argv, cwd, converter))
        evidence.mkdir()
        stdout = evidence / "stdout.log"
        stdout.write_text("")
        if converter == "databricks":
            assert argv[:5] == ["uv", "run", "--no-sync", "ossie-databricks", "deploy"]
            assert cwd == ROOT / "databricks"
            assert argv[argv.index("--catalog") + 1] == "catalog"
            assert argv[argv.index("--schema") + 1] == "scratch"
            prepared = yaml.safe_load(Path(argv[5]).read_text())
            write_deployment(prepared, Path(argv[-1]))
        else:
            assert argv[2] == "--databricks-queries"
            requested = json.loads(Path(argv[3]).read_text())
            assert "dataset:store_sales" in requested
            stdout.write_text(
                json.dumps(
                    {
                        k: [
                            "SELECT * FROM tpcds.public.store_sales",
                            [1] if failure == "params" else [],
                        ]
                        for k in requested
                    }
                )
            )
        return {
            "returncode": 1
            if (failure == "deploy" and converter == "databricks")
            or (failure == "compile" and converter == "cube")
            else 0,
            "stdout": str(stdout),
            "error": None,
        }

    def execute_statement(**kwargs):
        statement = kwargs["statement"]
        remote.append(statement)
        if statement.startswith("SELECT COUNT(*)"):
            return response([[10, 0]])
        if statement.startswith("WITH c"):
            width = 5 if "tpcds_retail_model__total_sales" in statement else 3
            return response([[1, 1, 0, *([0] * width)]])
        return response()

    monkeypatch.setitem(
        sys.modules,
        "databricks.sdk",
        SimpleNamespace(
            WorkspaceClient=lambda: SimpleNamespace(
                statement_execution=SimpleNamespace(execute_statement=execute_statement)
            )
        ),
    )
    runner = SimpleNamespace(
        root=ROOT,
        output=output,
        manifest=manifest,
        timeout=30,
        command=command,
        report={
            "cases": [
                {
                    "id": "cube--tpcds--to--databricks",
                    "tier": "A",
                    "grade": "LOSSY",
                    "output": str(exported),
                }
            ]
        },
    )
    result = run_check(runner)
    assert result["grade"] == ("FAIL" if failure else "OK"), result
    assert len(calls) == (0 if failure == "export" else 1 if failure == "deploy" else 2)
    assert bool(remote) == (failure is None)
    if not failure:
        assert len([s for s in remote if s.startswith("CREATE")]) == 5
        assert len([s for s in remote if s.startswith("DROP")]) == 5
