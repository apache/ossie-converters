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
"""Expression helpers shared by both conversion directions.

Lightdash SQL snippets reference the current model as ``${TABLE}.column``,
sibling fields as ``${column}`` or ``${metric}``, joined models as
``${other_table.column}`` and project parameters or user attributes as
``${lightdash.…}`` or ``${ld.…}``. Ossie expressions reference columns as
``dataset.column``. These helpers translate between the two forms and
recognize the aggregation shapes that map to Lightdash's typed metrics.
"""

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import sqlglot
from sqlglot import exp

_SQLGLOT_DIALECTS = {
    "BIGQUERY": "bigquery",
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
}

_REFERENCE_RE = re.compile(r"\$\{([^}]+)\}")
_NON_PORTABLE_REFERENCE_RE = re.compile(r"\$\{(?:lightdash|ld)\.[^}]*\}")
# Lightdash evaluates Liquid tags, and the bare `ld.parameters.x`,
# `ld.query.filters`, and `ld.user.x` references inside them, at query time.
_NON_PORTABLE_SYNTAX_RE = re.compile(
    r"\{%|\{\{|(?<![\w.])(?:lightdash|ld)\.(?:parameters|query|user)\."
)
_COLUMN_REFERENCE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.\w+)?$")

# Lightdash metric types that the Ossie expression encodes as an aggregation.
# Any other type exports as a `number` metric with raw SQL.
_AGGREGATE_FUNCTIONS = {
    "sum": "SUM",
    "min": "MIN",
    "max": "MAX",
    "average": "AVG",
    "median": "MEDIAN",
    "count": "COUNT",
}
_DISTINCT_AGGREGATE_FUNCTIONS = {
    "count_distinct": "COUNT",
    "sum_distinct": "SUM",
    "average_distinct": "AVG",
}
AGGREGATE_TYPES = frozenset(
    {*_AGGREGATE_FUNCTIONS, *_DISTINCT_AGGREGATE_FUNCTIONS, "percentile"}
)

_FUNCTION_TO_TYPE = {
    "SUM": "sum",
    "MIN": "min",
    "MAX": "max",
    "AVG": "average",
    "AVERAGE": "average",
    "MEDIAN": "median",
    "COUNT": "count",
}
_DISTINCT_FUNCTION_TO_TYPE = {
    "COUNT": "count_distinct",
    "SUM": "sum_distinct",
    "AVG": "average_distinct",
    "AVERAGE": "average_distinct",
}

_CALL_RE = re.compile(r"^\s*(?P<func>[A-Za-z_]+)\s*\((?P<body>.*)\)\s*$", re.DOTALL)
_DISTINCT_RE = re.compile(r"^DISTINCT\s+(?P<inner>.+)$", re.IGNORECASE | re.DOTALL)
_PERCENTILE_RE = re.compile(
    r"^\s*PERCENTILE_CONT\s*\(\s*(?P<fraction>[0-9]*\.?[0-9]+)\s*\)\s*"
    r"WITHIN\s+GROUP\s*\(\s*ORDER\s+BY\s+(?P<inner>.+?)\s*\)\s*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ParsedAggregation:
    lightdash_type: str
    inner: str
    percentile: Optional[float] = None


def _is_single_call_body(body: str) -> bool:
    """Checks whether the parentheses in ``body`` are balanced, that is,
    whether the parentheses around it belong to one function call."""
    depth = 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def parse_aggregation(expression: str) -> Optional[ParsedAggregation]:
    """Parses an expression that is exactly one aggregation over an operand.

    ``SUM(x)``, ``COUNT(DISTINCT x)``, ``SUM(DISTINCT x)``, and
    ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY x)`` map to Lightdash's typed
    metrics. The operand can be a bare column reference or any expression.

    Returns:
        The parsed aggregation, or None for any other shape (``COUNT(*)``,
        arithmetic between aggregations, unknown functions).
    """
    percentile_match = _PERCENTILE_RE.match(expression)
    if percentile_match:
        percentile = float(percentile_match.group("fraction")) * 100
        if percentile.is_integer():
            percentile = int(percentile)
        return ParsedAggregation(
            "percentile", percentile_match.group("inner").strip(), percentile
        )

    match = _CALL_RE.match(expression)
    if match is None or not _is_single_call_body(match.group("body")):
        return None
    function = match.group("func").upper()
    inner = match.group("body").strip()
    distinct = _DISTINCT_RE.match(inner)
    if distinct:
        lightdash_type = _DISTINCT_FUNCTION_TO_TYPE.get(function)
        inner = distinct.group("inner").strip()
    else:
        lightdash_type = _FUNCTION_TO_TYPE.get(function)
    if lightdash_type is None or not inner or inner == "*":
        return None
    return ParsedAggregation(lightdash_type, inner)


def build_aggregation(
    lightdash_type: str, inner: str, percentile: Optional[float] = None
) -> Optional[str]:
    """Builds the Ossie expression for a typed Lightdash metric over ``inner``.

    Returns:
        The expression, or None for a metric type that is not an aggregation
        (``number``, ``string``, ``boolean``, and so on).
    """
    if lightdash_type == "percentile":
        fraction = (50 if percentile is None else percentile) / 100
        return f"PERCENTILE_CONT({fraction:g}) WITHIN GROUP (ORDER BY {inner})"
    if lightdash_type in _DISTINCT_AGGREGATE_FUNCTIONS:
        return f"{_DISTINCT_AGGREGATE_FUNCTIONS[lightdash_type]}(DISTINCT {inner})"
    function = _AGGREGATE_FUNCTIONS.get(lightdash_type)
    if function is None:
        return None
    return f"{function}({inner})"


def is_column_reference(expression: str) -> bool:
    """Checks whether ``expression`` is a bare ``column`` or ``qualifier.column`` reference."""
    return _COLUMN_REFERENCE_RE.match(expression) is not None


def strip_qualifier(column_ref: str) -> str:
    """Gets the bare column name of a ``column`` or ``qualifier.column`` reference."""
    return column_ref.rsplit(".", 1)[-1]


def qualifier_of(column_ref: str) -> Optional[str]:
    """Gets the qualifier of a ``qualifier.column`` reference, or None."""
    if "." in column_ref:
        return column_ref.rsplit(".", 1)[0]
    return None


def ossie_sql_to_lightdash(
    expression: str, dataset: str, references: Optional[Dict[str, str]] = None
) -> str:
    """Rewrites Ossie column references into Lightdash references.

    ``dataset.column`` becomes ``${TABLE}.column``. A dataset in
    ``references`` (joined from ``dataset``) becomes
    ``${<join reference>.column}``, where the join reference is the joined
    model's name or its alias.
    """
    # The dataset's own mapping is applied last, so a join reference can't
    # override it. In a self-join (`employees` joined as `manager`), the
    # model's own columns stay `${TABLE}.col`.
    names = {**(references or {}), dataset: None}

    def replace(match: "re.Match[str]") -> str:
        name, column = match.group(1), match.group(2)
        reference = names[name]
        if reference is None:
            return f"${{TABLE}}.{column}"
        return f"${{{reference}.{column}}}"

    alternatives = "|".join(re.escape(name) for name in names)
    return re.sub(rf"\b({alternatives})\.(\w+)", replace, expression)


def has_non_portable_reference(sql: str) -> bool:
    """Checks whether the SQL depends on Lightdash query-time evaluation:
    project parameters, user attributes, or Liquid templating."""
    return (
        _NON_PORTABLE_REFERENCE_RE.search(sql) is not None
        or _NON_PORTABLE_SYNTAX_RE.search(sql) is not None
    )


@dataclass
class RewriteResult:
    expression: str
    inlined_metrics: List[str] = field(default_factory=list)
    flattened_aliases: List[str] = field(default_factory=list)


def lightdash_sql_to_ossie(
    sql: str,
    dataset: str,
    *,
    aliases: Optional[Dict[str, str]] = None,
    resolve_metric: Optional[Callable[[str], Optional[str]]] = None,
) -> RewriteResult:
    """Rewrites Lightdash references into Ossie ``dataset.column`` references.

    ``${TABLE}.column`` and bare ``${column}`` refer to the current model.
    ``${other_table.column}`` refers to a joined model and becomes a
    cross-dataset reference. ``${alias.column}`` is rewritten to the joined
    model. ``${metric}`` is replaced by that metric's expression when
    ``resolve_metric`` returns one. Parameter and user-attribute references
    are left unchanged; call ``has_non_portable_reference`` first.
    """
    alias_map = aliases or {}
    result = RewriteResult(expression="")

    def replace(match: "re.Match[str]") -> str:
        reference = match.group(1)
        if _NON_PORTABLE_REFERENCE_RE.match(match.group(0)):
            return match.group(0)
        if reference == "TABLE":
            return dataset
        if reference.startswith("TABLE."):
            return f"{dataset}.{reference[len('TABLE.'):]}"
        if "." in reference:
            table, column = reference.split(".", 1)
            if table in alias_map:
                result.flattened_aliases.append(table)
                return f"{alias_map[table]}.{column}"
            return reference
        if resolve_metric is not None:
            resolved = resolve_metric(reference)
            if resolved is not None:
                result.inlined_metrics.append(reference)
                return f"({resolved})"
        return f"{dataset}.{reference}"

    result.expression = _REFERENCE_RE.sub(replace, sql)
    return result


def qualify_bare_columns(sql: str, dataset: str, columns, *, dialect: str = "ANSI_SQL") -> str:
    """Prefixes bare references to the model's own columns with the dataset name.

    Lightdash SQL can name a column of the current model without ``${TABLE}``
    (``SUM(budget_use)``). Ossie consumers need ``dataset.column``. The parser
    distinguishes column references from types, date parts, and qualified
    paths. The prefix is inserted at the original identifier offset rather
    than by rendering the syntax tree, so dialect syntax, comments, and
    whitespace are preserved.

    Raises:
        ValueError: If the SQL is not a single expression or an identifier
            has no source position. sqlglot raises its own error for syntax
            it can't parse. The caller reports either instead of guessing.
    """
    lookup = {column.lower() for column in columns}
    if not lookup:
        return sql
    statements = sqlglot.parse(sql, read=_SQLGLOT_DIALECTS.get(dialect))
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Expected a single SQL expression")
    offsets = set()
    for column in statements[0].find_all(exp.Column):
        if column.table or column.name.lower() not in lookup:
            continue
        # A nested query has its own column scope. Don't bind its names to the
        # enclosing Lightdash model.
        if column.find_ancestor(exp.Query):
            continue
        start = column.this.meta.get("start")
        if start is None:
            raise ValueError("Column source position is unavailable")
        offsets.add(start)
    for start in sorted(offsets, reverse=True):
        sql = sql[:start] + dataset + "." + sql[start:]
    return sql


def referenced_datasets(expression: str, dataset_names: set) -> set:
    """Gets the subset of ``dataset_names`` that ``expression`` references."""
    found = set()
    for match in re.finditer(r"([A-Za-z_]\w*)\.\w+", expression):
        if match.group(1) in dataset_names:
            found.add(match.group(1))
    return found
