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

"""Compare the TPC-DS Metric View with Cube SQL on a Databricks SQL warehouse."""

import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

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


class LiveCheckError(Exception):
    pass


def cube_requests():
    return {
        grain: {
            "measures": [f"{CUBE_VIEW}.{measure}" for measure in MEASURES],
            "dimensions": [f"{CUBE_VIEW}.{field}"] if field else [],
        }
        for grain, field in GRAINS.items()
    }


def metric_view_definition(text, source_schema):
    view = yaml.safe_load(text)
    if (
        not isinstance(view, dict)
        or view.get("source") != FIXTURE_SOURCE + "store_sales"
        or not set(MEASURES) <= {m["name"] for m in view.get("measures", [])}
        or not {"i_brand", "s_store_name"}
        <= {field["name"] for field in view.get("dimensions", [])}
    ):
        raise LiveCheckError(
            "Databricks export lacks the expected TPC-DS source or members"
        )
    view["source"] = source_schema + ".store_sales"
    for join in view.get("joins", []):
        source = join["source"]
        if not source.startswith(FIXTURE_SOURCE):
            raise LiveCheckError(
                "Databricks export has an unexpected TPC-DS join source"
            )
        table = source_schema + "." + source.removeprefix(FIXTURE_SOURCE)
        if (
            source_schema.lower() == DEFAULT_SOURCE
            and source == FIXTURE_SOURCE + "date_dim"
        ):
            # The fixture exposes a month name that the Databricks sample table omits.
            table = (
                f"SELECT *, date_format(d_date, 'MMMM') AS d_month_name FROM {table}"
            )
        join["source"] = table
    return yaml.safe_dump(view, sort_keys=False)


def comparison_sql(cube_sql, view, field):
    selected = f"`{field}`, " if field else ""
    grouping = f" GROUP BY `{field}`" if field else ""
    measures = ", ".join(f"MEASURE(`{measure}`) AS `{measure}`" for measure in MEASURES)
    join = f"c.`{CUBE_VIEW}__{field}` <=> m.`{field}`" if field else "TRUE"
    counts = [
        "COUNT(c.present) AS cube_rows",
        "COUNT(m.present) AS metric_view_rows",
        "COUNT_IF(c.present IS NULL OR m.present IS NULL) AS missing_groups",
    ]
    for measure in MEASURES:
        left = f"c.`{CUBE_VIEW}__{measure}`"
        right = f"m.`{measure}`"
        if measure in ("customer_lifetime_value", "store_productivity"):
            # Allow small differences in the engine's decimal division and rounding.
            different = (
                f"(({left} IS NULL) <> ({right} IS NULL) OR "
                f"ABS({left} - {right}) > GREATEST(1E-8, ABS({left}) * 1E-8))"
            )
        else:
            different = f"NOT ({left} <=> {right})"
        counts.append(
            f"COUNT_IF(c.present IS NOT NULL AND m.present IS NOT NULL AND {different}) "
            f"AS `{measure}`"
        )
    return (
        f"WITH c AS (SELECT *, 1 AS present FROM ({cube_sql}) AS cube_query),\n"
        f"m AS (SELECT {selected}{measures}, 1 AS present FROM {view}{grouping})\n"
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
        error = response.status.error
        raise LiveCheckError(error.message if error else response.status.state.value)
    return response.result.data_array if response.result else None


def compare(api, warehouse, name, definition, queries, timeout, result):
    stage = "create metric view"
    try:
        execute(
            api,
            warehouse,
            f"CREATE VIEW {name} WITH METRICS LANGUAGE YAML AS $$\n{definition}$$",
            timeout,
        )
        for grain, field in GRAINS.items():
            stage = f"compare {grain} grain"
            rows = execute(
                api, warehouse, comparison_sql(queries[grain], name, field), timeout
            )
            if not rows or len(rows) != 1 or len(rows[0]) != 3 + len(MEASURES):
                raise LiveCheckError("unexpected comparison result shape")
            cube_rows, metric_view_rows, missing, *counts = map(int, rows[0])
            result["grains"][grain] = {
                "cube_rows": cube_rows,
                "metric_view_rows": metric_view_rows,
                "missing_groups": missing,
                "measure_mismatches": dict(zip(MEASURES, counts)),
            }
        if any(
            not summary["cube_rows"]
            or not summary["metric_view_rows"]
            or summary["missing_groups"]
            or any(summary["measure_mismatches"].values())
            for summary in result["grains"].values()
        ):
            result.update(grade="FAIL", reason="empty or differing query results")
    except (LiveCheckError, TypeError, ValueError) as error:
        result.update(grade="FAIL", reason=f"{stage}: {error}")
    finally:
        try:
            execute(api, warehouse, f"DROP VIEW IF EXISTS {name}", timeout)
        except Exception as error:
            result["cleanup_error"] = (
                f"could not remove {name} ({type(error).__name__})"
            )
            if result["grade"] == "OK":
                result.update(grade="FAIL", reason=result["cleanup_error"])


def run_check(runner):
    result = {
        "grade": "OK",
        "reason": "",
        "commands": [],
        "diagnostics": [],
        "grains": {},
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
        definition = metric_view_definition(
            Path(acceptance["output"]).read_text(), source
        )
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError:
            raise LiveCheckError(
                "install the optional databricks-sdk (uv run --with databricks-sdk)"
            ) from None
        fixture = runner.manifest.converters["cube"].fixtures["tpcds"]
        files = sorted([*fixture.rglob("*.yml"), *fixture.rglob("*.yaml")])
        directory = runner.output / "live-databricks"
        directory.mkdir(exist_ok=True)
        requests = directory / "cube-requests.json"
        requests.write_text(json.dumps(cube_requests(), indent=2) + "\n")
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
        for grain in GRAINS:
            sql, params = generated[grain]
            if params or FIXTURE_SOURCE not in sql:
                raise LiveCheckError(f"unexpected Cube SQL for the {grain} grain")
            queries[grain] = sql.replace(FIXTURE_SOURCE, source + ".")
        if "$$" in definition:
            raise LiveCheckError(
                "Metric View definition contains the Databricks YAML delimiter"
            )
        name = f"{scratch}.ossie_tpcds_parity_{uuid4().hex}"
        try:
            api = WorkspaceClient().statement_execution
        except Exception as error:
            raise LiveCheckError(
                f"Databricks authentication failed ({type(error).__name__})"
            ) from None
        compare(
            api,
            warehouse,
            name,
            definition,
            queries,
            runner.timeout,
            result,
        )
    except (
        LiveCheckError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        yaml.YAMLError,
    ) as error:
        result.update(grade="FAIL", reason=str(error))
    except Exception as error:
        # Authentication or SDK transport errors may carry configuration details.
        result.update(
            grade="FAIL",
            reason=f"Databricks SDK request failed ({type(error).__name__})",
        )
    return result
