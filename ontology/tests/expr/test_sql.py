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

"""`SqlExpressionParser`: the scalar SQL a dataset field's expression holds."""

from __future__ import annotations

import pytest

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
    SqlExpressionParser,
    SqlOp,
    SqlSyntaxError,
    parse_field_expression,
)
from ossie_ontology.model import Dataset, DatasetField, DialectExpression, DialectExpressionSet


@pytest.fixture(scope="module")
def parse():
    return SqlExpressionParser().parse


# ---------------------------------------------------------------------------
# Columns and literals
# ---------------------------------------------------------------------------

def test_a_bare_column(parse):
    assert parse("L_LINESTATUS") == Column("L_LINESTATUS")


@pytest.mark.parametrize("text, name, identifier, stored", [
    ("l_linestatus", "l_linestatus", "l_linestatus", "L_LINESTATUS"),
    ('"L_LINESTATUS"', "L_LINESTATUS", '"L_LINESTATUS"', "L_LINESTATUS"),
    ('"airport_code"', "airport_code", '"airport_code"', "airport_code"),
    ('"say ""hi"""', 'say "hi"', '"say ""hi"""', 'say "hi"'),
    ("amount$usd", "amount$usd", "amount$usd", "AMOUNT$USD"),
    ("CancelationCodé", "CancelationCodé", "CancelationCodé", "CANCELATIONCODÉ"),
])
def test_column_identifiers_keep_snowflake_case_rules(parse, text, name, identifier, stored):
    """Unquoted names fold to upper case when stored; quoted ones are exact."""
    column = parse(text)
    assert column == Column(name, quoted=text.startswith('"'))
    assert column.identifier == identifier
    assert column.stored_name == stored


@pytest.mark.parametrize("text, value", [
    ("42", 42),
    ("31.4", 31.4),
    ("-5", -5),
    ("'it''s'", "it's"),
    ("''", ""),
    ("TRUE", True),
    ("false", False),
])
def test_literals(parse, text, value):
    assert parse(text) == Literal(value)


def test_null(parse):
    assert parse("NULL") == Null()


# ---------------------------------------------------------------------------
# Operators and precedence
# ---------------------------------------------------------------------------

def test_is_null_and_is_not_null(parse):
    assert parse('"L_LINESTATUS" IS NULL') == IsNull(Column("L_LINESTATUS", quoted=True))
    assert parse("a is not null") == IsNull(Column("a"), negated=True)


@pytest.mark.parametrize("text, op", [
    ("a = 1", SqlOp.EQ), ("a <> 1", SqlOp.NE), ("a != 1", SqlOp.NE),
    ("a < 1", SqlOp.LT), ("a <= 1", SqlOp.LE), ("a > 1", SqlOp.GT), ("a >= 1", SqlOp.GE),
])
def test_comparisons(parse, text, op):
    assert parse(text) == Binary(op, Column("a"), Literal(1))


def test_multiplication_binds_tighter_than_addition(parse):
    a, b, c = Column("a"), Column("b"), Column("c")
    assert parse("a + b * c") == Binary(SqlOp.PLUS, a, Binary(SqlOp.TIMES, b, c))
    assert parse("(a + b) * c") == Binary(SqlOp.TIMES, Binary(SqlOp.PLUS, a, b), c)
    assert parse("a - b - c") == Binary(SqlOp.MINUS, Binary(SqlOp.MINUS, a, b), c)


def test_concatenation_is_left_associative_at_the_additive_level(parse):
    first, last = Column("c_first_name"), Column("c_last_name")
    assert parse("c_first_name || ' ' || c_last_name") == Binary(
        SqlOp.CONCAT, Binary(SqlOp.CONCAT, first, Literal(" ")), last
    )


def test_unary_minus_on_a_column_is_a_negation(parse):
    assert parse("-a * b") == Binary(SqlOp.TIMES, Negate(Column("a")), Column("b"))


def test_not_and_or_bind_in_sql_order(parse):
    """NOT binds tighter than AND, which binds tighter than OR."""
    a_eq, b_null, c_ne = (Binary(SqlOp.EQ, Column("a"), Literal(1)), IsNull(Column("b"), negated=True),
                          Binary(SqlOp.NE, Column("c"), Literal(2)))
    assert parse("NOT a = 1 AND b IS NOT NULL OR c <> 2") == Or(And(Not(a_eq), b_null), c_ne)


def test_function_calls_are_parsed_for_the_target_to_judge(parse):
    assert parse("upper(name)") == Call("UPPER", (Column("name"),))
    assert parse("now()") == Call("NOW")


@pytest.mark.parametrize("text, expected", [
    ("a", False), ("a + 1", False), ("a || b", False),
    ("a = 1", True), ("a IS NULL", True), ("NOT a", True), ("a = 1 OR b = 2", True),
])
def test_is_predicate(parse, text, expected):
    assert parse(text).is_predicate is expected


def test_columns_are_listed_once_each_in_order(parse):
    """`"A"` and `a` are one stored column, A, so it is listed once."""
    assert [c.identifier for c in parse('b + "A" * a + b').columns()] == ["b", '"A"']


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, message", [
    ("CASE WHEN a = 1 THEN 'x' END", r"'CASE' is not supported"),
    ("x + CAST(a AS int)", r"'CAST' is not supported"),
    ("a IN (1, 2)", r"'IN' is not supported"),
    ("a::int", r"'::' casts are not supported"),
    ("t.a", r"Qualified column names are not supported"),
    ("a ==", r"Unexpected '=' at position 3"),
    ("a = b = c", r"Unexpected '=' at position 6"),
    ('"unterminated', r"Illegal character '\"' at position 0"),
    ("", r"Unexpected end of expression"),
])
def test_what_is_outside_the_subset_is_rejected_by_name(parse, text, message):
    with pytest.raises(SqlSyntaxError, match=message):
        parse(text)


# ---------------------------------------------------------------------------
# Reading a field
# ---------------------------------------------------------------------------

def _field(name: str, *dialects: tuple[str, str]) -> DatasetField:
    field = DatasetField(
        name=name,
        expression=DialectExpressionSet([DialectExpression(d, e) for d, e in dialects]),
    )
    Dataset("LINEITEM", "DB.S.LINEITEM", [field])
    return field


def test_a_field_is_read_in_the_first_preferred_dialect_it_has():
    field = _field("missing", ("ANSI_SQL", "l_linestatus IS NULL"), ("SNOWFLAKE", '"L_LINESTATUS" IS NULL'))
    assert parse_field_expression(field, ("SNOWFLAKE", "ANSI_SQL")) == IsNull(Column("L_LINESTATUS", quoted=True))
    assert parse_field_expression(field, ("POSTGRES",)) == IsNull(Column("l_linestatus"))


def test_a_field_with_no_dialect_reads_the_column_it_is_named_after():
    assert parse_field_expression(_field("cashReceived")) == Column("cashReceived")


def test_a_syntax_error_names_the_field():
    with pytest.raises(SqlSyntaxError, match=r"^Field 'LINEITEM.status': 'CASE' is not supported"):
        parse_field_expression(_field("status", ("ANSI_SQL", "CASE WHEN a THEN b END")))
