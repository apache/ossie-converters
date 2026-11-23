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

"""Deploy an Ossie model onto Unity Catalog metric views.

Each dataset becomes the metric view `<catalog>.<schema>.<dataset>`, and the model is rewritten
so that each dataset reads its metric view. Relationships and model-scoped metrics stay in the
model unchanged, with a warning for each object not deployed. Model metrics must still
resolve against the fields and measures exposed by the generated views.
"""

import copy
from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from ossie_databricks import _ossie, metric_view
from ossie_databricks.expressions import (
    DATABRICKS,
    OSSIE,
    Untranslatable,
    columns,
    identifier,
    measure_reference,
    parse,
)
from ossie_databricks.result import ConversionError, Result, WarningCode


def deploy_ossie_model_to_databricks(
    ossie_model: str | Mapping[str, Any], *, catalog: str, schema: str
) -> Result[dict[str, str]]:
    """The deployment files, `{relative path: YAML text}`.

    `metric_views/<dataset>.yaml` holds the metric view of each dataset; its first line names the
    target, `<catalog>.<schema>.<dataset>`. `<model>.ossie.yaml` holds the rewritten model.
    """
    model = _ossie.load(ossie_model)
    _ossie.check(model, "the input")
    result: Result[dict[str, str]] = Result({})
    deployed = copy.deepcopy(model)
    _reject_source_target_collisions(deployed, catalog=catalog, schema=schema)
    files = {}
    exposed_datasets = []
    for dataset in deployed["datasets"]:
        name = dataset["name"]
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ConversionError(f"the dataset name {name!r} cannot be a file name")
        view = metric_view.from_dataset(dataset, result)
        view_table = exp.table_(name, db=schema, catalog=catalog)
        view_name = view_table.sql(dialect="databricks")
        files[f"metric_views/{name}.yaml"] = f"# Metric view: {view_name}\n{_ossie.dump(view)}"
        dataset["source"] = view_name
        # An object without a dimension or measure keeps its expression as it is.
        dimensions = {item["name"] for item in view.get("dimensions") or []}
        measures = {item["name"] for item in view.get("measures") or []}
        exposed_datasets.append(
            {
                "name": name,
                "fields": [{"name": item["name"]} for item in view.get("dimensions") or []],
                "metrics": [{"name": item["name"]} for item in view.get("measures") or []],
            }
        )
        for field in dataset.get("fields") or []:
            if field["name"] in dimensions:
                field["expression"] = {"sql": identifier(field["name"], OSSIE)}
        for metric in dataset.get("metrics") or []:
            if metric["name"] in measures:
                path = f"datasets[{name}].metrics[{metric['name']}]"
                metric["expression"] = _deployed_metric(metric, dataset, path, result)
    # The core validator currently reports many unresolved model-metric references as
    # warnings. Deployment must not ignore them: the rewritten sources expose only the
    # generated dimensions and measures, never the original tables' other columns.
    problems = _ossie.model_metric_reference_problems(
        {
            "name": deployed["name"],
            "datasets": exposed_datasets,
            "metrics": deployed.get("metrics") or [],
        }
    )
    if problems:
        raise ConversionError(
            "model-scoped metrics cannot be evaluated over the metric views:\n  "
            + "\n  ".join(problems)
            + "\nExpose the referenced fields and dataset metrics in the metric views "
            "before deploying."
        )
    for metric in deployed.get("metrics") or []:
        result.warn(
            WarningCode.NOT_DEPLOYED,
            f"metrics[{metric['name']}]",
            "model-scoped metric is retained in the Ossie model; "
            "no metric-view measure is generated",
        )
    for relationship in deployed.get("relationships") or []:
        result.warn(
            WarningCode.NOT_DEPLOYED,
            f"relationships[{relationship['name']}]",
            "relationship is retained in the Ossie model; no metric-view join is generated",
        )
    _ossie.check(deployed, "the deployed model")
    files[f"{model['name']}.ossie.yaml"] = _ossie.dump(deployed)

    result.value = files
    return result


def _reject_source_target_collisions(
    model: Mapping[str, Any], *, catalog: str, schema: str
) -> None:
    """Reject deploying a dataset onto a table that any dataset source reads.

    A metric view must not read its own target. That includes a plain table source equal to
    the target and a query source whose FROM/JOIN reads the target table.
    """
    targets = {}
    for dataset in model["datasets"]:
        view_table = exp.table_(dataset["name"], db=schema, catalog=catalog)
        targets[_table_key(view_table)] = view_table.sql(dialect="databricks")
    for dataset in model["datasets"]:
        for table in _source_tables(dataset.get("source")):
            target = targets.get(_table_key(table))
            if target is not None:
                raise ConversionError(
                    f"the metric view target {target} is read by a dataset source; "
                    "deploy to a different catalog or schema"
                )


def _table_key(table: exp.Table) -> tuple[str, str, str]:
    return (table.catalog.lower(), table.db.lower(), table.name.lower())


def _is_three_part(table: Any) -> bool:
    return (
        isinstance(table, exp.Table)
        and len(table.parts) == 3
        and all(isinstance(part, exp.Identifier) for part in table.parts)
    )


def _source_tables(source: Any) -> list[exp.Table]:
    """The three-part tables a dataset source reads, whether a table name or a query."""
    if not isinstance(source, str):
        return []
    try:
        table = sqlglot.parse_one(source, read="databricks", into=exp.Table)
        if _is_three_part(table):
            return [table]
    except (ParseError, TokenError):
        pass
    try:
        tree = sqlglot.parse_one(source, read="databricks")
    except (ParseError, TokenError):
        return []
    return [table for table in tree.find_all(exp.Table) if _is_three_part(table)]


def _deployed_metric(
    metric: Mapping[str, Any], dataset: Mapping[str, Any], path: str, result: Result
) -> dict[str, Any]:
    """Evaluate the measure, and keep the portable text when it still holds over the view.

    The view exposes the fields of the dataset and no source columns.
    """
    fields = {field["name"] for field in dataset.get("fields") or []}

    def holds(text: str, dialect: str) -> bool:
        try:
            tree = parse(text, dialect)
        except Untranslatable:
            return False
        return all(c.name in fields and c.table in ("", dataset["name"]) for c in columns(tree))

    old = metric["expression"]
    portable = [
        e for e in old.get("dialects") or [] if e["dialect"] in metric_view.PORTABLE_DIALECTS
    ]
    deployed: dict[str, Any] = {}
    if old.get("sql") and holds(old["sql"], OSSIE):
        deployed["sql"] = old["sql"]
    kept = [e for e in portable if holds(e["expression"], e["dialect"])]
    if (old.get("sql") or portable) and not (deployed or kept):
        result.warn(
            WarningCode.METRIC_NOT_PORTABLE,
            path,
            "the portable expression reads source columns, which the metric view does not "
            "expose; the metric only evaluates the measure",
        )
    evaluate = {"dialect": DATABRICKS, "expression": measure_reference(metric["name"], DATABRICKS)}
    deployed["dialects"] = [*kept, evaluate]
    return deployed
