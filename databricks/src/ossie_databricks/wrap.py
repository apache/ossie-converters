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

"""Wrap an existing metric view in a one-dataset Ossie model that reads the metric view."""

from collections.abc import Iterable, Mapping
from typing import Any

from sqlglot import exp
from sqlglot.errors import ParseError

from ossie_databricks import _ossie, metric_view
from ossie_databricks.expressions import (
    DATABRICKS,
    OSSIE,
    Untranslatable,
    identifier,
    measure_reference,
    parse,
)
from ossie_databricks.result import ConversionError, Result, WarningCode


def wrap_metric_view_in_ossie_model(
    metric_view_name: str,
    metric_view_yaml: str | Mapping[str, Any],
    *,
    dataset_name: str | None = None,
) -> Result[str]:
    """A model, as YAML, whose one dataset has the metric view `metric_view_name` as its source.

    Fields name the dimensions. Each metric evaluates its measure with `MEASURE()`, and also
    has a portable expression when the measure reads only dimensions.
    """
    view = metric_view.load(metric_view_yaml)
    try:
        table = exp.to_table(metric_view_name, dialect="databricks")
    except ParseError as error:
        raise ConversionError(f"invalid metric view name {metric_view_name!r}") from error
    name = dataset_name or table.name
    path = f"metric_views[{name}]"
    result: Result[str] = Result("")
    if view.get("parameters"):
        result.warn(
            WarningCode.METRIC_VIEW_HAS_PARAMETERS,
            path,
            "portable expressions use the parameter defaults",
        )

    dimensions = view.get("dimensions") or view.get("fields") or []
    fields = [
        _ossie_item(dimension, {"sql": identifier(dimension["name"], OSSIE)}, is_field=True)
        for dimension in dimensions
    ]
    rename = _measure_inputs(dimensions, view)
    metrics = []
    for measure in view.get("measures") or []:
        item_path = f"{path}.measures[{measure['name']}]"
        expression: dict[str, Any] = {}
        try:
            if measure.get("window"):
                raise Untranslatable("a window measure")
            expression["sql"] = metric_view.portable_sql(measure["expr"], rename)
        except Untranslatable as error:
            message = f"evaluates the measure only: {error}"
            result.warn(WarningCode.METRIC_NOT_PORTABLE, item_path, message)
        evaluate = measure_reference(measure["name"], DATABRICKS)
        expression["dialects"] = [{"dialect": DATABRICKS, "expression": evaluate}]
        metrics.append(_ossie_item(measure, expression, is_field=False))

    dataset: dict[str, Any] = {"name": name, "source": metric_view_name}
    if view.get("comment"):
        dataset["description"] = view["comment"]
    if fields:
        dataset["fields"] = fields
    if metrics:
        dataset["metrics"] = metrics
    model = {"version": _ossie.spec_version(), "name": name, "datasets": [dataset]}
    _ossie.check(model, "the wrapped model")
    result.value = _ossie.dump(model)
    return result


def _ossie_item(
    item: Mapping[str, Any], expression: dict[str, Any], *, is_field: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {"name": item["name"], "expression": expression}
    # An Ossie metric has no label; the display name stays in the metric view.
    if is_field and item.get("display_name"):
        out["label"] = item["display_name"]
    if item.get("comment"):
        out["description"] = item["comment"]
    if item.get("synonyms"):
        out["ai_context"] = {"synonyms": list(item["synonyms"])}
    return out


def _measure_inputs(dimensions: list[Mapping[str, Any]], view: Mapping[str, Any]):
    """Map each column that a measure reads to the dimension that exposes it.

    A measure is portable over the dataset only when every input is a dimension: the dataset
    exposes the dimensions of the metric view and nothing else.
    """
    names = {dimension["name"] for dimension in dimensions}
    exposed = {}
    for dimension in dimensions:
        try:
            tree = parse(dimension["expr"], DATABRICKS)
        except Untranslatable:
            continue
        if isinstance(tree, exp.Column):
            exposed.setdefault(_key(tree), dimension["name"])
    fan_out = set(_one_to_many_joins(view.get("joins") or [], inherited=False))
    parameters = metric_view.parameter_values(view)

    def rename(column: exp.Column) -> exp.Expression | None:
        if column.table in fan_out:
            # A one_to_many join is aggregated at the grain of the source, not row by row.
            raise Untranslatable(f"{column.sql()} reads the one_to_many join {column.table}")
        if not column.table and column.name in names:
            return exp.column(column.name)
        if _key(column) in exposed:
            return exp.column(exposed[_key(column)])
        if not column.table and column.name in parameters:
            return parameters[column.name]
        raise Untranslatable(f"{column.sql()} is not a dimension of the metric view")

    return rename


def _key(column: exp.Column) -> tuple[str, str]:
    table = "" if column.table == "source" else column.table
    return table.lower(), column.name.lower()


def _one_to_many_joins(joins: Iterable[Mapping[str, Any]], *, inherited: bool) -> Iterable[str]:
    # Joins below a one_to_many join are one_to_many too.
    for join in joins:
        fans_out = inherited or join.get("cardinality") == "one_to_many"
        if fans_out:
            yield join["name"]
        yield from _one_to_many_joins(join.get("joins") or [], inherited=fans_out)
