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

"""What a dataset field reads, parsed from the SQL its `expression` holds.

A field's `name` is what mappings call it; its `expression` is what it reads
from the dataset's source -- a column that may be named differently
(`line_status` reads `L_LINESTATUS`), or a computation over columns
(`missing_line_status` is `"L_LINESTATUS" IS NULL`). Anything that has to touch
the source must therefore go through the expression, never the name.
"""

from __future__ import annotations

from typing import Sequence

from ossie_ontology.expr.sql.model import (
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
)
from ossie_ontology.expr.sql.parser import SqlExpressionParser, SqlSyntaxError
from ossie_ontology.model import DatasetField

__all__ = [
    "And",
    "Binary",
    "Call",
    "Column",
    "IsNull",
    "Literal",
    "Negate",
    "Not",
    "Null",
    "Or",
    "SqlNode",
    "SqlOp",
    "SqlExpressionParser",
    "SqlSyntaxError",
    "field_expression",
    "parse_field_expression",
]

_parser: SqlExpressionParser | None = None


def field_expression(field: DatasetField, dialects: Sequence[str] = ()) -> str | None:
    """The SQL text of *field*, in the first of *dialects* it is written in.

    Falls back to the field's first dialect when it has none of them, and to
    None when it has no dialect at all.
    """
    for dialect in dialects:
        chosen = field.expression.by_dialect(dialect)
        if chosen is not None:
            return chosen.expression
    primary = field.expression.primary
    return primary.expression if primary is not None else None


def parse_field_expression(field: DatasetField, dialects: Sequence[str] = ()) -> SqlNode:
    """Parse *field*'s expression, preferring the first of *dialects* it has.

    A field written in no dialect at all -- the core spec requires one, but an
    `expression: {}` still loads -- reads the column it is named after, which
    is what its name was taken to mean before expressions were read.

    Raises SqlSyntaxError naming the field when the expression is outside the
    SQL subset `SqlExpressionParser` covers.
    """
    global _parser
    text = field_expression(field, dialects)
    if text is None:
        return Column(field.name)
    if _parser is None:
        _parser = SqlExpressionParser()
    try:
        return _parser.parse(text)
    except SqlSyntaxError as e:
        raise SqlSyntaxError(f"Field '{_qualified(field)}': {e}") from e


def _qualified(field: DatasetField) -> str:
    return f"{field.dataset.name}.{field.name}" if field.dataset is not None else field.name
