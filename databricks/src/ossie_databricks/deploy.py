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
so that each dataset reads its metric view.
"""

import copy
from collections.abc import Mapping
from typing import Any

from sqlglot import exp

from ossie_databricks import _ossie, metric_view, star
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
    ossie_model: str | Mapping[str, Any], *, catalog: str, schema: str, joins: bool = False
) -> Result[dict[str, str]]:
    """The deployment files, `{relative path: YAML text}`.

    `metric_views/<dataset>.yaml` holds the metric view of each dataset; its first line names the
    target, `<catalog>.<schema>.<dataset>`. `<model>.ossie.yaml` holds the rewritten model.
    """
    model = _ossie.load(ossie_model)
    _ossie.check(model, "the input")
    result: Result[dict[str, str]] = Result({})
    deployed = copy.deepcopy(model)
    files = {}
    exposed = {}
    datasets = {dataset["name"].casefold(): dataset for dataset in model["datasets"]}
    views = {name: metric_view.from_dataset(dataset, result) for name, dataset in datasets.items()}
    stars = {}
    deployed_metrics = {}
    if joins:
        import_root, extras = star.imported_options(model)
        for name in datasets:
            stars[name] = star.Star(
                name,
                datasets,
                model.get("relationships") or [],
                result,
                extras if name == import_root else {},
            )
        deployed_metrics = star.add_model_metrics(model.get("metrics") or [], stars, views, result)
    joined = {name for item in stars.values() for name in item.relationships}
    for dataset in deployed["datasets"]:
        name = dataset["name"]
        if "/" in name or "\\" in name or name in (".", ".."):
            raise ConversionError(f"the dataset name {name!r} cannot be a file name")
        view = views[name.casefold()]
        root_dimensions = {item["name"] for item in view.get("dimensions") or []}
        if joins:
            stars[name.casefold()].add_dimensions(view, result)
            stars[name.casefold()].add_imported_measures(view, result)
            stars[name.casefold()].restore_options(view)
        view_name = exp.table_(name, db=schema, catalog=catalog).sql(dialect="databricks")
        files[f"metric_views/{name}.yaml"] = f"# Metric view: {view_name}\n{_ossie.dump(view)}"
        dataset["source"] = view_name
        # An object without a dimension or measure keeps its expression as it is.
        dimensions = root_dimensions
        exposed[name.casefold()] = {field.casefold() for field in dimensions}
        measures = {item["name"] for item in view.get("measures") or []}
        for field in dataset.get("fields") or []:
            if field["name"] in dimensions:
                field["expression"] = {"sql": identifier(field["name"], OSSIE)}
        for metric in dataset.get("metrics") or []:
            if metric["name"] in measures:
                path = f"datasets[{name}].metrics[{metric['name']}]"
                metric["expression"] = _deployed_metric(metric, dataset, path, result)
    _report_omissions(
        deployed, exposed, result, joined, deployed_metrics, datasets if joins else {}
    )
    _ossie.check(deployed, "the deployed model")
    files[f"{model['name']}.ossie.yaml"] = _ossie.dump(deployed)

    result.value = files
    return result


def _report_omissions(
    model: dict,
    exposed: dict[str, set[str]],
    result: Result,
    joined: set[str],
    deployed_metrics: dict[str, str],
    join_datasets: dict[str, dict],
) -> None:
    for relationship in model.get("relationships") or []:
        if relationship["name"] in joined:
            continue
        message = "the relationship is absent from the metric views"
        if join_datasets and not star.unique_target(
            relationship, join_datasets[relationship["to"].casefold()]
        ):
            message += " because its target columns do not form a declared unique key"
        result.warn(WarningCode.NOT_DEPLOYED, f"relationships[{relationship['name']}]", message)
    metrics = model.get("metrics") or []
    names = {metric["name"].casefold() for metric in metrics}
    dependencies = {}
    removed = set()
    for metric in metrics:
        name = metric["name"].casefold()
        dependencies[name] = set()
        if name in deployed_metrics:
            ref = exp.Anonymous(
                this="MEASURE",
                expressions=[exp.column(metric["name"], table=deployed_metrics[name])],
            )
            metric["expression"] = {
                "dialects": [{"dialect": DATABRICKS, "expression": ref.sql(dialect="databricks")}]
            }
            continue
        for text, dialect in _sql_entries(metric["expression"]):
            try:
                refs = columns(parse(text, dialect))
            except Untranslatable:
                removed.add(name)
                continue
            for column in refs:
                parts = [part.name.casefold() for part in column.parts]
                if len(parts) == 1 and parts[0] in names:
                    dependencies[name].add(parts[0])
                elif len(parts) < 2 or parts[1] not in exposed.get(parts[0], ()):
                    removed.add(name)
    while new := {
        name for name, refs in dependencies.items() if name not in removed and refs & removed
    }:
        removed.update(new)
    for metric in metrics:
        if metric["name"].casefold() in deployed_metrics:
            continue
        message = "the model metric is absent from the metric views"
        if metric["name"].casefold() in removed:
            message += " and was removed from the rewritten model because its SQL cannot resolve"
        result.warn(WarningCode.NOT_DEPLOYED, f"metrics[{metric['name']}]", message)
    if removed:
        kept = [metric for metric in metrics if metric["name"].casefold() not in removed]
        if kept:
            model["metrics"] = kept
        else:
            model.pop("metrics")


def _sql_entries(expression: Mapping[str, Any]):
    if expression.get("sql"):
        yield expression["sql"], OSSIE
    for entry in expression.get("dialects") or []:
        dialect = entry["dialect"]
        if metric_view._parsable(dialect) and not (dialect == OSSIE and expression.get("sql")):
            yield entry["expression"], dialect


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
