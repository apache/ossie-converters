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

"""Metric view definitions: build one from an Ossie dataset, and read one.

Name resolution in metric view expressions:
https://docs.databricks.com/aws/en/metric-views/data-modeling/
"""

from collections.abc import Callable, Mapping
from typing import Any

from sqlglot import exp

from ossie_databricks import _ossie
from ossie_databricks.expressions import (
    DATABRICKS,
    OSSIE,
    Untranslatable,
    has_window,
    measure_names,
    parse,
    translate,
)
from ossie_databricks.result import ConversionError, Result, WarningCode

VERSION = "1.1"

# Metric view limits for agent metadata.
_MAX_SYNONYMS = 10
_MAX_TEXT = 255

# Portable dialects, in the order they are preferred when an object has several.
PORTABLE_DIALECTS = (OSSIE, "ANSI_SQL")

Rename = Callable[[exp.Column], exp.Expression | None]


def renamed(tree: exp.Expression, rename: Rename) -> exp.Expression:
    def visit(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            return rename(node) or node
        return node

    return tree.transform(visit)


def from_dataset(dataset: Mapping[str, Any], result: Result) -> dict[str, Any]:
    """The metric view of an Ossie dataset: fields become dimensions, metrics become measures.

    An object without a SQL expression has no dimension or measure, and is reported.
    """
    path = f"datasets[{dataset['name']}]"
    view: dict[str, Any] = {
        "version": VERSION,
        "source": dataset["source"].strip().removesuffix(";").rstrip(),
    }
    if dataset.get("description"):
        view["comment"] = dataset["description"]
    fields = dataset.get("fields") or []
    metrics = dataset.get("metrics") or []
    names = {item["name"] for item in [*fields, *metrics]}

    def dimension_input(own: str) -> Rename:
        # In a dimension an unqualified name resolves to an earlier dimension first, while in
        # Ossie every name of a field expression is a source column.
        others = names - {own}
        return lambda c: exp.column(c.name, "source") if not c.table and c.name in others else None

    def measure_input(column: exp.Column) -> exp.Expression | None:
        # Inside a metric view the dataset is implicit.
        return exp.column(column.name) if column.table == dataset["name"] else None

    dimensions = []
    for field in fields:
        item_path = f"{path}.fields[{field['name']}]"
        expr = _databricks_text(
            field["expression"], dimension_input(field["name"]), item_path, result
        )
        if expr is not None:
            dimensions.append(_item(field, expr, item_path, result))
    measures = []
    for metric in metrics:
        item_path = f"{path}.metrics[{metric['name']}]"
        expr = _databricks_text(metric["expression"], measure_input, item_path, result)
        if expr is not None:
            measures.append(_item(metric, expr, item_path, result))
    if dimensions:
        view["dimensions"] = dimensions
    if measures:
        view["measures"] = measures
    return view


def portable_text(expression: Mapping[str, Any]) -> tuple[str, str] | None:
    """The portable text and its dialect: `sql`, then a portable dialect entry."""
    if expression.get("sql"):
        return expression["sql"], OSSIE
    entries = {entry["dialect"]: entry["expression"] for entry in expression.get("dialects") or []}
    for dialect in PORTABLE_DIALECTS:
        if dialect in entries:
            return entries[dialect], dialect
    return None


def _databricks_text(
    expression: Mapping[str, Any], rename: Rename, path: str, result: Result
) -> str | None:
    entries = {entry["dialect"]: entry["expression"] for entry in expression.get("dialects") or []}
    if DATABRICKS in entries:
        return entries[DATABRICKS]
    picked = portable_text(expression) or next(
        ((text, dialect) for dialect, text in entries.items() if _parsable(dialect)), None
    )
    if picked is None:
        result.warn(
            WarningCode.NO_SQL_EXPRESSION, path, "not in the metric view: no SQL expression"
        )
        return None
    text, dialect = picked
    try:
        return translate(renamed(parse(text, dialect), rename), DATABRICKS)
    except Untranslatable as error:
        result.warn(WarningCode.NOT_TRANSLATED, path, f"{error}; the {dialect} text is kept")
        return text


def _parsable(dialect: str) -> bool:
    try:
        _ossie.sqlglot_dialect(dialect)
    except KeyError:
        return False
    return True


def _item(obj: Mapping[str, Any], expr: str, path: str, result: Result) -> dict[str, Any]:
    item: dict[str, Any] = {"name": obj["name"], "expr": expr}
    if obj.get("description"):
        item["comment"] = obj["description"]
    if obj.get("label"):
        item["display_name"] = obj["label"][:_MAX_TEXT]
    ai_context = obj.get("ai_context")
    synonyms = ai_context.get("synonyms") if isinstance(ai_context, dict) else None
    if synonyms:
        kept = [synonym[:_MAX_TEXT] for synonym in synonyms[:_MAX_SYNONYMS]]
        if kept != synonyms:
            result.warn(
                WarningCode.SYNONYMS_TRUNCATED,
                path,
                f"a metric view keeps at most {_MAX_SYNONYMS} synonyms of {_MAX_TEXT} characters",
            )
        item["synonyms"] = kept
    return item


def load(definition: str | Mapping[str, Any]) -> dict[str, Any]:
    view = _ossie.load(definition)
    # YAML reads `version: 1.1` as a number.
    if str(view.get("version")) != VERSION:
        raise ConversionError(
            f"metric view version {view.get('version')!r} is not supported; only {VERSION} is"
        )
    if not isinstance(view.get("source"), str):
        raise ConversionError("a metric view needs a source")
    for parameter in view.get("parameters") or []:
        if "default" not in parameter:
            raise ConversionError(
                f"the parameter {parameter['name']} has no default, so the metric view has no "
                "value without one"
            )
    return view


def parameter_values(view: Mapping[str, Any]) -> dict[str, exp.Expression]:
    """Each parameter default as a literal."""
    values = {}
    for parameter in view.get("parameters") or []:
        default = parameter["default"]
        if isinstance(default, bool):
            values[parameter["name"]] = exp.Boolean(this=default)
        elif isinstance(default, int | float):
            values[parameter["name"]] = exp.Literal.number(default)
        else:
            values[parameter["name"]] = exp.Literal.string(str(default))
    return values


def portable_sql(text: str, rename: Rename) -> str:
    """The OSSIE_SQL_2026 form of a Databricks expression, with its columns renamed."""
    tree = parse(text, DATABRICKS)
    if measure_names(tree):
        raise Untranslatable("it reads another measure")
    if has_window(tree):
        raise Untranslatable("it has a window function")
    return translate(renamed(tree, rename), OSSIE)
