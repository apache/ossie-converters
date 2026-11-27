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

"""Shared helpers for building/reading Ossie ``OssieExpression`` values from Sigma formulas."""

from __future__ import annotations

from typing import Optional

import sqlglot
from ossie import OssieDialect, OssieDialectExpression, OssieExpression
from sqlglot import Dialect, expressions as exp

from ossie_sigma.sigma_formula import (
    BinOp,
    ColumnRef,
    FormulaNode,
    FormulaParseError,
    FuncCall,
    UnaryOp,
    parse_formula,
    to_ansi_sql,
)


def qualify(node: FormulaNode, table_name: str) -> FormulaNode:
    """Rewrite every unqualified :class:`ColumnRef` in *node* to reference *table_name*.

    Sigma metric formulas are scoped to their owning element and reference sibling
    columns unqualified (e.g. ``Sum([Amount])``); Ossie metrics live at the model
    level and may span datasets via relationships, so their expressions must be
    fully dataset-qualified.
    """
    if isinstance(node, ColumnRef):
        return node if node.table is not None else ColumnRef(table_name, node.column)
    if isinstance(node, UnaryOp):
        return UnaryOp(node.op, qualify(node.operand, table_name))
    if isinstance(node, BinOp):
        return BinOp(
            node.op, qualify(node.left, table_name), qualify(node.right, table_name)
        )
    if isinstance(node, FuncCall):
        return FuncCall(node.name, tuple(qualify(a, table_name) for a in node.args))
    return node


def _inline_field_sources(
    node: FormulaNode,
    sources: dict[str, str],
    active: tuple[str, ...],
    dataset_alias: Optional[str],
    source_alias: Optional[str],
    budget: list[int],
) -> FormulaNode:
    """Lower Sigma sibling-column reads to source expressions for Ossie fields."""
    budget[0] -= 1
    if budget[0] < 0:
        raise FormulaParseError(
            "Sigma sibling expansion exceeds the portable expression budget"
        )
    if isinstance(node, ColumnRef):
        if node.table is not None and node.table == source_alias:
            return ColumnRef(None, node.column)
        if node.table is None or node.table == dataset_alias:
            formula = sources.get(node.column)
            if formula is not None:
                if node.column in active:
                    plain = parse_formula(formula)
                    if node.column == active[-1] and isinstance(plain, ColumnRef):
                        return ColumnRef(None, plain.column)
                    raise FormulaParseError("cyclic Sigma column dependency")
                return _inline_field_sources(
                    parse_formula(formula),
                    sources,
                    (*active, node.column),
                    dataset_alias,
                    source_alias,
                    budget,
                )
            return ColumnRef(None, node.column)
        return node
    if isinstance(node, UnaryOp):
        return UnaryOp(
            node.op,
            _inline_field_sources(
                node.operand, sources, active, dataset_alias, source_alias, budget
            ),
        )
    if isinstance(node, BinOp):
        return BinOp(
            node.op,
            _inline_field_sources(
                node.left, sources, active, dataset_alias, source_alias, budget
            ),
            _inline_field_sources(
                node.right, sources, active, dataset_alias, source_alias, budget
            ),
        )
    if isinstance(node, FuncCall):
        return FuncCall(
            node.name,
            tuple(
                (
                    _inline_field_sources(
                        arg, sources, active, dataset_alias, source_alias, budget
                    )
                    for arg in node.args
                )
            ),
        )
    return node


def build_expression(
    formula: str,
    dataset_alias: Optional[str] = None,
    *,
    field_sources: Optional[dict[str, str]] = None,
    field_name: Optional[str] = None,
    source_alias: Optional[str] = None,
) -> OssieExpression:
    """Build an :class:`OssieExpression` from a raw Sigma formula.

    Always includes a ``SIGMA``-dialect entry carrying the original formula text
    verbatim (guaranteeing lossless round-tripping), plus an ``ANSI_SQL`` entry when
    the formula translates cleanly.
    """
    dialects = [OssieDialectExpression(dialect=OssieDialect.SIGMA, expression=formula)]
    try:
        node = parse_formula(formula)
        if field_sources is not None:
            node = _inline_field_sources(
                node, field_sources, (field_name,), dataset_alias, source_alias, [4096]
            )
        sql = to_ansi_sql(node, dataset_alias=dataset_alias)
    except (FormulaParseError, RecursionError):
        sql = None
    if sql is not None:
        dialects.append(
            OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=sql)
        )
    return OssieExpression(dialects=dialects)


def sigma_dialect_text(expression: OssieExpression) -> Optional[str]:
    """Return the raw Sigma formula text from *expression*, if a ``SIGMA`` dialect entry exists."""
    for dialect_expr in expression.dialects or []:
        if dialect_expr.dialect == OssieDialect.SIGMA:
            return dialect_expr.expression
    return None


# SQLGlot has no ANSI dialect. Select the generic reader explicitly for portable SQL.
SQL_DIALECT_READERS = {
    OssieDialect.ANSI_SQL: Dialect,
    OssieDialect.OSSIE_SQL_2026: Dialect,
    OssieDialect.DATABRICKS: "databricks",
    OssieDialect.SNOWFLAKE: "snowflake",
    OssieDialect.BIGQUERY: "bigquery",
}


def preferred_sql_expression(
    expression: OssieExpression,
) -> Optional[OssieDialectExpression]:
    """Select SQL for both metric ownership and formula translation."""
    if expression.sql is not None:
        return OssieDialectExpression(
            dialect=OssieDialect.OSSIE_SQL_2026, expression=expression.sql
        )
    for dialect in SQL_DIALECT_READERS:
        for candidate in expression.dialects or []:
            if candidate.dialect == dialect:
                return candidate
    return None


def infer_single_dataset_qualifier(
    sql: str, dataset_names: set[str], *, dialect: str | type[Dialect] | None = Dialect
) -> Optional[str]:
    """Return the sole known dataset referenced by *sql*'s qualified columns, if unambiguous.

    Used to place a model-level metric with no preserved Sigma ``element_id`` (i.e. one
    authored by, or round-tripped through, a non-Sigma tool) back onto a single Sigma
    element — Sigma metrics are always scoped to one element, unlike Ossie metrics,
    which may span datasets via relationships.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return None

    qualifiers = {column.table for column in tree.find_all(exp.Column) if column.table}
    return (
        qualifiers.pop()
        if len(qualifiers) == 1 and qualifiers <= dataset_names
        else None
    )
