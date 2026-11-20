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

"""Dataset fields as PyRel: what a field's SQL expression reads, over its table.

A field is read through its `expression`, never its `name`: `line_status` is
the table's `L_LINESTATUS` column, and `missing_line_status` is not a column at
all but `"L_LINESTATUS" IS NULL`. Two things come from that expression here:

  * `table_schema` -- the columns a dataset's table has to declare, keyed by
    the identifiers the expressions use, so a declared table names the columns
    the warehouse actually has;
  * `render_field` -- the PyRel a mapping binds for the field: a column, a
    computation over columns, or, for a condition, a `ConditionValue` that is
    TRUE on the rows where it holds. Nothing is asserted for the other rows:
    under PyRel's closed-world reading, no fact means not TRUE.
"""

from __future__ import annotations

import operator
from functools import reduce
from typing import Any, Callable

from relationalai.semantics import Concept as RAIConcept, not_, union
from relationalai.semantics.std import strings

from ossie_ontology.expr.sql import (
    And,
    Binary,
    Call,
    Column,
    IsNull,
    Literal,
    Negate,
    Not,
    Null,
    Or,
    SqlNode,
    SqlOp,
    field_expression,
    parse_field_expression,
)
from ossie_ontology.model import Dataset, DatasetField
from ossie_relationalai.mappings import ConditionValue

# RelationalAI reads its tables from Snowflake, so a field written in that
# dialect is read in it, and in ANSI SQL otherwise.
DIALECTS = ("SNOWFLAKE", "ANSI_SQL")

_OPERATORS: dict[SqlOp, Callable[[Any, Any], Any]] = {
    SqlOp.PLUS: operator.add,
    SqlOp.MINUS: operator.sub,
    SqlOp.TIMES: operator.mul,
    SqlOp.DIVIDE: operator.truediv,
    SqlOp.MODULO: operator.mod,
    SqlOp.EQ: operator.eq,
    SqlOp.NE: operator.ne,
    SqlOp.LT: operator.lt,
    SqlOp.LE: operator.le,
    SqlOp.GT: operator.gt,
    SqlOp.GE: operator.ge,
}


class UnsupportedFieldExpression(ValueError):
    """A field's SQL parses, but has no PyRel rendering here."""


def field_sql(field: DatasetField) -> SqlNode:
    return parse_field_expression(field, DIALECTS)


def table_schema(dataset: Dataset, concept: Callable[[str], RAIConcept]) -> dict[str, RAIConcept]:
    """Column identifier -> type, for every column *dataset*'s fields read.

    A field that is a bare column gives that column the builtin its `datatype`
    holds; one with no datatype, or none with a builtin (`Time`, `Opaque`), is
    declared `String`, and so is a column that is only ever read inside a
    computation, since nothing says what it holds. Two fields that read the same
    column as different types contradict each other, and are rejected.
    """
    schema: dict[str, RAIConcept] = {}
    # stored column name -> (type it is declared as, field that declared it)
    declared: dict[str, tuple[str, str]] = {}
    computed: list[Column] = []
    for fl in dataset.fields:
        sql = field_sql(fl)
        if not isinstance(sql, Column):
            computed.extend(sql.columns())
            continue
        type_name = (fl.datatype.builtin_name if fl.datatype is not None else None) or "String"
        prior = declared.get(sql.stored_name)
        if prior is None:
            declared[sql.stored_name] = (type_name, fl.name)
            schema[sql.identifier] = concept(type_name)
        elif prior[0] != type_name:
            raise ValueError(
                f"Fields '{dataset.name}.{prior[1]}' and '{dataset.name}.{fl.name}' both read "
                f"column {sql.stored_name}, as '{prior[0]}' and '{type_name}'"
            )
    for col in computed:
        if col.stored_name not in declared:
            declared[col.stored_name] = ("String", "")
            schema[col.identifier] = concept("String")
    return schema


def render_field(field: DatasetField, table) -> Any:
    """The PyRel *field* reads from *table*.

    A value expression renders to the PyRel expression over the table's columns.
    A condition renders to a ConditionValue: TRUE where the condition holds.
    """
    sql = field_sql(field)
    try:
        if sql.is_predicate:
            return ConditionValue(_conditions(sql, table, holds=True))
        return _value(sql, table)
    except ValueError as e:
        # Unsupported SQL, or a column the table does not have: either way the
        # field is what the reader has to go and fix.
        name = f"{field.dataset.name}.{field.name}" if field.dataset is not None else field.name
        raise type(e)(f"Field '{name}' ({field_expression(field, DIALECTS)!r}): {e}") from e


def _value(node: SqlNode, table) -> Any:
    if isinstance(node, Column):
        return _column(node, table)
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Null):
        raise UnsupportedFieldExpression("NULL has no value to read")
    if isinstance(node, Negate):
        return -_value(node.operand, table)
    if isinstance(node, Binary) and node.op is SqlOp.CONCAT:
        return strings.concat(*(_value(part, table) for part in _concat_parts(node)))
    if isinstance(node, Binary) and not node.op.is_comparison:
        return _OPERATORS[node.op](_value(node.left, table), _value(node.right, table))
    if isinstance(node, Call):
        raise UnsupportedFieldExpression(f"function {node.name} is not supported")
    # A condition nested in a value, `(a = b) + 1`: it only has a rendering as
    # a field's whole expression, where it becomes a ConditionValue.
    raise UnsupportedFieldExpression(f"condition {node} is only supported as the whole expression")


def _conditions(node: SqlNode, table, holds: bool) -> list:
    """PyRel conditions that are all true exactly when *node* is TRUE (or FALSE).

    Only TRUE is ever bound, but FALSE is still needed underneath a NOT: in SQL
    `NOT (a = 1)` is TRUE where `a = 1` is FALSE, which is `a <> 1` -- not
    "anywhere `a = 1` is not TRUE", since a NULL `a` makes both UNKNOWN.
    Comparisons get that for free, because PyRel only compares values that are
    present.
    """
    if isinstance(node, Binary) and node.op.is_comparison:
        op = node.op if holds else node.op.negated()
        return [_OPERATORS[op](_value(node.left, table), _value(node.right, table))]
    if isinstance(node, IsNull):
        value = _value(node.operand, table)
        return [not_(value)] if holds != node.negated else [_present(value)]
    if isinstance(node, Not):
        return _conditions(node.operand, table, not holds)
    if isinstance(node, (And, Or)):
        both = isinstance(node, And) == holds
        left = _conditions(node.left, table, holds)
        right = _conditions(node.right, table, holds)
        return left + right if both else [union(_all(left), _all(right))]
    if isinstance(node, Column):
        # A Boolean column used as a condition.
        return [_column(node, table) == holds]
    if isinstance(node, Literal) and isinstance(node.value, bool):
        if node.value == holds:
            return []
        raise UnsupportedFieldExpression(f"a condition that is never {'TRUE' if holds else 'FALSE'}")
    raise UnsupportedFieldExpression(f"{node} is not a condition")


def _column(node: Column, table) -> Any:
    try:
        return table[node.identifier]
    except KeyError as e:
        raise ValueError(f"column {node.identifier} is not in table '{table._name}': {e.args[0]}") from e


def _present(value) -> Any:
    """A condition that holds when *value* has one: SQL's `IS NOT NULL`.

    Spelled as a double negation rather than the bare value. A bare column in a
    `where` is the same condition to the compiled model, but PyRel's decompiler
    drops it, so the generated source would claim the rule holds for every row.
    """
    return not_(not_(value))


def _all(conditions: list) -> Any:
    """One condition that holds when all of *conditions* do, for a `union` branch."""
    if not conditions:
        raise UnsupportedFieldExpression("a condition that is always TRUE")
    return reduce(operator.and_, conditions)


def _concat_parts(node: SqlNode) -> list[SqlNode]:
    """`a || b || c` as [a, b, c], for one `concat` call rather than nested ones."""
    if isinstance(node, Binary) and node.op is SqlOp.CONCAT:
        return _concat_parts(node.left) + _concat_parts(node.right)
    return [node]
