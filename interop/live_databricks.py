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

"""Opt-in TPC-DS checks of dataset-scoped Databricks deployment assets.

Cube supplies model-level SQL; this is not a general Ossie query executor.
"""

import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import sqlglot
from sqlglot import exp
import yaml

from .execution import succeeded

MEASURES = (
    "total_sales",
    "total_profit",
    "sales_by_brand",
    "customer_lifetime_value",
    "store_productivity",
)
GRAINS = {"total": None, "brand": "i_brand", "store": "s_store_name"}
CUBE_VIEW = "tpcds_retail_model"
DEFAULT_SOURCE = "samples.tpcds_sf1"
FIXTURE_SOURCE = "tpcds.public."
SCHEMA = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")
ROW_COUNT = "ossie_parity_row_count"
DATASETS = {"store_sales", "date_dim", "customer", "item", "store"}


class LiveCheckError(Exception):
    pass


def cube_requests(model=None):
    requests = {
        grain: {
            "measures": [f"{CUBE_VIEW}.{measure}" for measure in MEASURES],
            "dimensions": [f"{CUBE_VIEW}.{field}"] if field else [],
        }
        for grain, field in GRAINS.items()
    }
    for dataset in (model or {}).get("datasets", []):
        metrics = [
            m["name"] for m in dataset.get("metrics", []) if m["name"] != ROW_COUNT
        ]
        if metrics:
            requests["dataset:" + dataset["name"]] = {
                "measures": [f"{dataset['name']}.{m}" for m in metrics],
                "dimensions": [],
            }
    return requests


def prepare_model(text, source_schema):
    """Adapt the real matrix Ossie export, adding only test instrumentation."""
    model = yaml.safe_load(text)
    if (
        not isinstance(model, dict)
        or {d["name"] for d in model.get("datasets", [])} != DATASETS
    ):
        raise LiveCheckError(
            "requires the current TPC-DS Ossie export, not Metric View YAML"
        )
    names = {m["name"] for m in model.get("metrics", [])}
    names.update(m["name"] for d in model["datasets"] for m in d.get("metrics", []))
    if not set(MEASURES) <= names:
        raise LiveCheckError("TPC-DS Ossie export lacks expected metrics")
    for dataset in model["datasets"]:
        name = dataset["name"]
        if dataset.get("source") != FIXTURE_SOURCE + name:
            raise LiveCheckError("unexpected TPC-DS dataset source")
        dataset["source"] = source_schema + "." + name
        if source_schema.lower() == DEFAULT_SOURCE and name == "date_dim":
            # Databricks' public sample omits this fixture column.
            dataset["source"] = (
                "SELECT *, date_format(d_date, 'MMMM') AS d_month_name FROM "
                + dataset["source"]
            )
        if ROW_COUNT in {
            m["name"] for m in dataset.get("metrics", []) + dataset.get("fields", [])
        }:
            raise LiveCheckError("TPC-DS instrumentation name collision")
        dataset.setdefault("metrics", []).append(
            {"name": ROW_COUNT, "expression": {"sql": "COUNT(*)"}}
        )
    return model


def deployment_plan(model, directory, scratch, token):
    """Read deploy CLI artifacts, then bind them to run-owned scratch names."""
    deployed = yaml.safe_load((directory / f"{model['name']}.ossie.yaml").read_text())
    for key in ("metrics", "relationships"):
        if deployed.get(key) != model.get(key):
            raise LiveCheckError(f"deployment changed model-scoped {key}")
    by_name = {d["name"]: d for d in deployed["datasets"]}
    if set(by_name) != DATASETS:
        raise LiveCheckError("deployment has missing or unexpected datasets")
    assets = []
    for dataset in model["datasets"]:
        name = dataset["name"]
        view = yaml.safe_load((directory / "metric_views" / f"{name}.yaml").read_text())
        output = by_name[name]
        if (
            output["source"] != f"{scratch}.{name}"
            or view["source"] != dataset["source"]
        ):
            raise LiveCheckError("deployment has an unexpected source binding")
        dimensions = {d["name"] for d in view.get("dimensions", [])}
        metrics = [m["name"] for m in dataset["metrics"]]
        if dimensions != {f["name"] for f in dataset["fields"]} or set(metrics) != {
            m["name"] for m in view.get("measures", [])
        }:
            raise LiveCheckError("deployment dropped dataset dimensions or measures")
        if {m["name"] for m in output.get("metrics", [])} != set(metrics):
            raise LiveCheckError("deployment dropped rewritten dataset metrics")
        for key in ("primary_key", "unique_keys"):
            if output.get(key) != dataset.get(key):
                raise LiveCheckError("deployment changed dataset keys")
        for metric in output.get("metrics", []):
            dialects = metric["expression"].get("dialects", [])
            if not any(
                d["dialect"] == "DATABRICKS" and "MEASURE(" in d["expression"]
                for d in dialects
            ):
                raise LiveCheckError("deployment lacks native MEASURE bindings")
        target = f"{scratch}.ossie_tpcds_{token}_{name}"
        output["source"] = target
        # The public fixture has identity source fields as well as a computed name.
        # Cube's native SQL still uses physical names, so expose only identity fields.
        columns = []
        for field in dataset["fields"]:
            expression = field["expression"]
            sql = expression.get("sql") or next(
                d["expression"]
                for d in expression.get("dialects", [])
                if d["dialect"] in ("OSSIE_SQL_2026", "ANSI_SQL")
            )
            parsed = sqlglot.parse_one(sql, read="databricks")
            if (
                isinstance(parsed, exp.Column)
                and not parsed.table
                and parsed.name == field["name"]
            ):
                columns.append(field["name"])
        if not columns:
            raise LiveCheckError("TPC-DS dataset has no identity source fields")
        selected = ", ".join(f"`{c}`" for c in columns)
        projection = (
            f"SELECT {selected}, MEASURE(`{ROW_COUNT}`) AS `{ROW_COUNT}` "
            f"FROM {target} GROUP BY {selected}"
        )
        definition = yaml.safe_dump(view, sort_keys=False)
        if "$$" in definition:
            raise LiveCheckError(
                "Metric View definition contains the Databricks YAML delimiter"
            )
        assets.append(
            {
                "dataset": name,
                "target": target,
                "definition": definition,
                "projection": projection,
                "metrics": [m for m in metrics if m != ROW_COUNT],
                "source": dataset["source"],
            }
        )
    # Save exactly the model bindings used by this run, alongside untouched CLI output.
    (directory / "live-bindings.ossie.yaml").write_text(
        yaml.safe_dump(deployed, sort_keys=False)
    )
    return assets


def bind_query(sql, assets, *, deployed):
    """Rebind table AST nodes only; leave aliases, literals and Cube calculations intact."""
    by_name = {a["dataset"]: a for a in assets}
    seen = set()

    def replace(node):
        if not isinstance(node, exp.Table) or (node.catalog, node.db) != (
            "tpcds",
            "public",
        ):
            return node
        if node.name not in by_name:
            raise LiveCheckError("Cube SQL reads an unexpected fixture table")
        asset = by_name[node.name]
        seen.add(node.name)
        source = asset["projection"] if deployed else asset["source"]
        if deployed or source.lstrip().upper().startswith("SELECT"):
            replacement = exp.Subquery(
                this=sqlglot.parse_one(source, read="databricks")
            )
        else:
            replacement = exp.to_table(source, dialect="databricks")
        replacement.set(
            "alias",
            node.args.get("alias").copy()
            if node.args.get("alias")
            else exp.TableAlias(this=node.this.copy()),
        )
        return replacement

    query = sqlglot.parse_one(sql, read="databricks").transform(replace)
    if not seen:
        raise LiveCheckError("Cube SQL does not read the expected TPC-DS source")
    return query.sql(dialect="databricks")


def comparison_sql(cube_sql, candidate_sql, field, measures=MEASURES, prefix=CUBE_VIEW):
    join = f"c.`{prefix}__{field}` <=> m.`{prefix}__{field}`" if field else "TRUE"
    counts = [
        "COUNT(c.present) AS cube_rows",
        "COUNT(m.present) AS metric_view_rows",
        "COUNT_IF(c.present IS NULL OR m.present IS NULL) AS missing_groups",
    ]
    for measure in measures:
        left, right = f"c.`{prefix}__{measure}`", f"m.`{prefix}__{measure}`"
        if measure in ("customer_lifetime_value", "store_productivity"):
            different = (
                f"(({left} IS NULL) <> ({right} IS NULL) OR "
                f"ABS({left} - {right}) > GREATEST(1E-8, ABS({left}) * 1E-8))"
            )
        else:
            different = f"NOT ({left} <=> {right})"
        counts.append(
            f"COUNT_IF(c.present IS NOT NULL AND m.present IS NOT NULL AND {different}) AS `{measure}`"
        )
    return (
        f"WITH c AS (SELECT *, 1 AS present FROM ({cube_sql}) AS cube_query),\n"
        f"m AS (SELECT *, 1 AS present FROM ({candidate_sql}) AS deployed_query)\n"
        f"SELECT {', '.join(counts)}\nFROM c FULL OUTER JOIN m ON {join}"
    )


def execute(api, warehouse, sql, timeout):
    deadline = time.monotonic() + timeout
    response = api.execute_statement(
        warehouse_id=warehouse, statement=sql, wait_timeout="0s"
    )
    while response.status.state.value in {"PENDING", "RUNNING"}:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            api.cancel_execution(response.statement_id)
            raise LiveCheckError("Databricks statement timed out")
        time.sleep(min(2, remaining))
        response = api.get_statement(response.statement_id)
    if response.status.state.value != "SUCCEEDED":
        # SDK error messages may include connection details and submitted SQL.
        raise LiveCheckError("Databricks statement " + response.status.state.value)
    return response.result.data_array if response.result else None


def record_comparison(rows, measures):
    if not rows or len(rows) != 1 or len(rows[0]) != 3 + len(measures):
        raise LiveCheckError("unexpected comparison result shape")
    cube_rows, metric_view_rows, missing, *counts = map(int, rows[0])
    return {
        "cube_rows": cube_rows,
        "metric_view_rows": metric_view_rows,
        "missing_groups": missing,
        "measure_mismatches": dict(zip(measures, counts)),
    }


def compare(api, warehouse, assets, queries, timeout, result):
    created = []
    stage = "create metric views"
    try:
        for asset in assets:
            execute(
                api,
                warehouse,
                f"CREATE VIEW {asset['target']} WITH METRICS LANGUAGE YAML AS $$\n{asset['definition']}$$",
                timeout,
            )
            created.append(asset["target"])
        for asset in assets:
            stage = "check row multiplicity: " + asset["dataset"]
            rows = execute(
                api,
                warehouse,
                f"SELECT COUNT(*), COUNT_IF(`{ROW_COUNT}` <> 1 OR `{ROW_COUNT}` IS NULL) FROM ({asset['projection']}) AS projected",
                timeout,
            )
            if not rows or len(rows) != 1 or len(rows[0]) != 2:
                raise LiveCheckError("unexpected row multiplicity result shape")
            count, duplicates = map(int, rows[0])
            result["dataset_rows"][asset["dataset"]] = count
            if count <= 0 or duplicates:
                raise LiveCheckError("empty or non-unique dataset projection")
            if asset["metrics"]:
                stage = "compare native measures: " + asset["dataset"]
                prefix = asset["dataset"]
                selected = ", ".join(
                    f"MEASURE(`{m}`) AS `{prefix}__{m}`" for m in asset["metrics"]
                )
                native = f"SELECT {selected} FROM {asset['target']}"
                reference = bind_query(
                    queries["dataset:" + prefix], assets, deployed=False
                )
                rows = execute(
                    api,
                    warehouse,
                    comparison_sql(reference, native, None, asset["metrics"], prefix),
                    timeout,
                )
                result["dataset_measures"][prefix] = record_comparison(
                    rows, asset["metrics"]
                )
        for grain, field in GRAINS.items():
            stage = f"compare {grain} grain"
            reference = bind_query(queries[grain], assets, deployed=False)
            candidate = bind_query(queries[grain], assets, deployed=True)
            rows = execute(
                api, warehouse, comparison_sql(reference, candidate, field), timeout
            )
            result["grains"][grain] = record_comparison(rows, MEASURES)
        summaries = [*result["grains"].values(), *result["dataset_measures"].values()]
        if any(
            not s["cube_rows"]
            or not s["metric_view_rows"]
            or s["missing_groups"]
            or any(s["measure_mismatches"].values())
            for s in summaries
        ):
            raise LiveCheckError("empty or differing query results")
    except (LiveCheckError, TypeError, ValueError) as error:
        result.update(grade="FAIL", reason=f"{stage}: {error}")
    finally:
        errors = []
        for name in reversed(created):
            try:
                execute(api, warehouse, f"DROP VIEW IF EXISTS {name}", timeout)
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            result["cleanup_error"] = (
                f"could not remove {len(errors)} run-owned Metric View(s): {', '.join(errors)}"
            )
            result.update(
                grade="FAIL", reason=result["reason"] or result["cleanup_error"]
            )


def run_check(runner):
    result = {
        "grade": "OK",
        "reason": "",
        "commands": [],
        "diagnostics": [],
        "grains": {},
        "dataset_rows": {},
        "dataset_measures": {},
    }
    try:
        source = os.environ.get("OSSIE_DATABRICKS_TPCDS_SCHEMA", DEFAULT_SOURCE)
        scratch = os.environ.get("OSSIE_DATABRICKS_SCRATCH_SCHEMA", "")
        warehouse = os.environ.get("OSSIE_DATABRICKS_WAREHOUSE_ID", "")
        if (
            not warehouse
            or not SCHEMA.fullmatch(scratch)
            or not SCHEMA.fullmatch(source)
        ):
            raise LiveCheckError(
                "set OSSIE_DATABRICKS_WAREHOUSE_ID and a catalog.schema in "
                "OSSIE_DATABRICKS_SCRATCH_SCHEMA (and OSSIE_DATABRICKS_TPCDS_SCHEMA if overriding the sample)"
            )
        acceptance = next(
            (
                case
                for case in runner.report["cases"]
                if case["id"] == "cube--tpcds--to--databricks" and case["tier"] == "A"
            ),
            None,
        )
        if not acceptance or acceptance["grade"] not in {"OK", "LOSSY"}:
            raise LiveCheckError(
                "requires a successful Cube TPC-DS to Databricks matrix export"
            )
        model = prepare_model(Path(acceptance["output"]).read_text(), source)
        directory = runner.output / "live-databricks"
        directory.mkdir()
        input_path = directory / "input.ossie.yaml"
        input_path.write_text(yaml.safe_dump(model, sort_keys=False))
        converter = runner.manifest.converters["databricks"]
        catalog, schema = scratch.split(".")
        deployment = directory / "deployment"
        stage = runner.command(
            result,
            [
                "uv",
                "run",
                "--no-sync",
                "ossie-databricks",
                "deploy",
                str(input_path),
                "--catalog",
                catalog,
                "--schema",
                schema,
                "-o",
                str(deployment),
            ],
            converter.cwd,
            directory / "deploy",
            "databricks",
        )
        if not succeeded(stage):
            raise LiveCheckError(
                "Databricks deployment failed; see live-databricks/deploy logs"
            )
        assets = deployment_plan(model, deployment, scratch, uuid4().hex)
        requests = directory / "cube-requests.json"
        requests.write_text(json.dumps(cube_requests(model), indent=2) + "\n")
        fixture = runner.manifest.converters["cube"].fixtures["tpcds"]
        files = sorted([*fixture.rglob("*.yml"), *fixture.rglob("*.yaml")])
        stage = runner.command(
            result,
            [
                "node",
                str(runner.root / "cube/tools/cube_compile.js"),
                "--databricks-queries",
                str(requests),
                *map(str, files),
            ],
            runner.root,
            directory / "cube-queries",
            "cube",
        )
        if not succeeded(stage):
            raise LiveCheckError(
                "Cube SQL generation failed; see live-databricks/cube-queries logs"
            )
        generated = json.loads(Path(stage["stdout"]).read_text())
        queries = {}
        for key in cube_requests(model):
            sql, params = generated[key]
            if params:
                raise LiveCheckError("unexpected parameters in Cube SQL")
            # Validate all bindings before creating any remote assets.
            bind_query(sql, assets, deployed=False)
            bind_query(sql, assets, deployed=True)
            queries[key] = sql
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError:
            raise LiveCheckError(
                "install the optional databricks-sdk (uv run --with databricks-sdk)"
            ) from None
        try:
            api = WorkspaceClient().statement_execution
        except Exception as error:
            raise LiveCheckError(
                f"Databricks authentication failed ({type(error).__name__})"
            ) from None
        compare(api, warehouse, assets, queries, runner.timeout, result)
    except (
        LiveCheckError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        yaml.YAMLError,
    ) as error:
        result.update(grade="FAIL", reason=str(error))
    except Exception as error:
        result.update(
            grade="FAIL",
            reason=f"Databricks SDK request failed ({type(error).__name__})",
        )
    return result
