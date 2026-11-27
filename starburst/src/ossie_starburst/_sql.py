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

"""Translate source references and SQL expressions without executing them."""

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError

TRINO = "TRINO"
PORTABLE = "OSSIE_SQL_2026"
_IDENTIFIER = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_]*)'
_SOURCE = re.compile(rf"{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER}){{2}}")
_PORTABLE_FUNCTIONS = {
    "ABS",
    "AVG",
    "CAST",
    "COALESCE",
    "COUNT",
    "DATE_TRUNC",
    "LOWER",
    "MAX",
    "MIN",
    "SUM",
    "TIMESTAMP_TRUNC",
    "UPPER",
}
_PORTABLE_CAST_TYPES = {
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.BOOLEAN,
    exp.DataType.Type.DATE,
    exp.DataType.Type.DECIMAL,
    exp.DataType.Type.DOUBLE,
    exp.DataType.Type.INT,
    exp.DataType.Type.TEXT,
    exp.DataType.Type.TIME,
    exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPNTZ,
    exp.DataType.Type.VARCHAR,
}


class ConversionError(ValueError):
    """The input cannot be converted without changing its meaning."""


def parse_source(source):
    if not _SOURCE.fullmatch(source.strip()):
        raise ConversionError(
            f"expected a catalog.schema.relation source, got {source!r}"
        )
    return tuple(
        name[1:-1].replace('""', '"') if name.startswith('"') else name.lower()
        for name in re.findall(_IDENTIFIER, source)
    )


def source_sql(parts):
    return ".".join(identifier(part).sql(dialect="trino") for part in parts)


def identifier(name):
    return exp.to_identifier(
        name, quoted=re.fullmatch(r"[a-z_][a-z_0-9]*", name) is None
    )


def reference(parts):
    return ".".join(part.replace("\\", "\\\\").replace(".", "\\.") for part in parts)


def parse_reference(value, size):
    parts = [""]
    escaped = False
    for char in value:
        if escaped:
            if char not in ".\\":
                raise ConversionError(f"invalid escape in reference {value!r}")
            parts[-1] += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ".":
            parts.append("")
        else:
            parts[-1] += char
    if escaped or len(parts) != size or any(not part.strip() for part in parts):
        raise ConversionError(
            f"expected a {size}-part Context Studio reference, got {value!r}"
        )
    return tuple(parts)


def parse_expression(sql, *, read="trino"):
    try:
        node = sqlglot.parse_one(sql, read=read, into=exp.Condition)
    except SqlglotError as error:
        raise ConversionError(f"invalid SQL expression {sql!r}: {error}") from error
    if any(
        isinstance(item, (exp.Query, exp.Block, exp.Lambda, exp.Window, exp.Alias))
        for item in node.walk()
    ):
        raise ConversionError(
            f"expected a single scalar or aggregate expression, got {sql!r}"
        )
    for function in node.find_all(exp.Func):
        name = (
            function.name
            if isinstance(function, exp.Anonymous)
            else function.sql_name()
        )
        if name.upper() == "AGG":
            raise ConversionError(
                "Ossie AGG measure evaluation has no Context Studio equivalent"
            )
    return node


def select_expression(expression):
    dialects = {
        item["dialect"].upper(): item["expression"]
        for item in expression.get("dialects", [])
    }
    if TRINO in dialects:
        return parse_expression(dialects[TRINO])
    sql = expression.get("sql", dialects.get(PORTABLE))
    if sql is not None:
        node = parse_expression(sql)
        check_portable(node)
        return node
    if "ANSI_SQL" in dialects:
        return parse_expression(dialects["ANSI_SQL"], read=None)
    raise ConversionError("expression needs TRINO, OSSIE_SQL_2026, or ANSI_SQL")


def check_portable(expression):
    for node in expression.walk():
        if isinstance(node, (exp.Div, exp.IntDiv)):
            raise ConversionError(
                "portable division needs an explicit TRINO expression"
            )
        if isinstance(node, exp.Cast) and node.to.this not in _PORTABLE_CAST_TYPES:
            raise ConversionError(
                f"CAST target {render(node.to)} needs an explicit TRINO expression"
            )
        if isinstance(node, exp.Func) and node.sql_name() not in _PORTABLE_FUNCTIONS:
            raise ConversionError(
                f"portable function {node.sql_name()} needs an explicit TRINO expression"
            )


def physical_expression(node, relation, *, qualify=False):
    def convert(column):
        parts = column.parts
        qualifiers = tuple(
            part.name if part.quoted else part.name.lower() for part in parts[:-1]
        )
        if (
            qualifiers
            and qualifiers != ("source",)
            and qualifiers != relation[-len(qualifiers) :]
        ):
            raise ConversionError(f"{column.sql()} references a different relation")
        return exp.Column(
            this=column.this.copy(), table=identifier("source") if qualify else None
        )

    return node.transform(
        lambda item: convert(item) if isinstance(item, exp.Column) else item
    )


def render(expression):
    try:
        return expression.sql(dialect="trino", unsupported_level=ErrorLevel.RAISE)
    except SqlglotError as error:
        raise ConversionError(f"cannot render Trino SQL: {error}") from error


def ossie_expression(node):
    sql = render(node)
    result = {"dialects": [{"dialect": TRINO, "expression": sql}]}
    try:
        check_portable(node)
    except ConversionError:
        return result
    result["sql"] = sql
    return result
