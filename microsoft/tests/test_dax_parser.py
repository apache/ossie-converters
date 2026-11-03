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

"""Tests for the DAX expression parser used by the DAX-to-OSSIE_SQL translator."""

import pytest

from ossie_microsoft._dax_parser import (
    Binary,
    Bracket,
    Call,
    Column,
    DaxParseError,
    Empty,
    Name,
    Number,
    String,
    Table,
    TableConstructor,
    Unary,
    Var,
    parse_dax,
)


def test_column_references_quoted_and_bare():
    assert parse_dax("'Order Lines'[Qty]") == Column("Order Lines", "Qty")
    assert parse_dax("Sales[Amount]") == Column("Sales", "Amount")


def test_escapes_in_names_and_strings():
    assert parse_dax("'O''Brien'[A]]B]") == Column("O'Brien", "A]B")
    assert parse_dax('"say ""hi"""') == String('say "hi"')


def test_unqualified_bracket_and_table_and_name():
    assert parse_dax("[Total Sales]") == Bracket("Total Sales")
    assert parse_dax("'Sales'") == Table("Sales")
    assert parse_dax("Sales") == Name("Sales")


def test_function_names_are_upper_cased_and_may_contain_dots():
    assert parse_dax("percentile.inc('S'[A], 0.5)") == Call(
        "PERCENTILE.INC", (Column("S", "A"), Number("0.5"))
    )


def test_empty_and_omitted_arguments():
    assert parse_dax("TODAY()") == Call("TODAY", ())
    assert parse_dax("F(1, , 3)") == Call("F", (Number("1"), Empty(), Number("3")))


def test_multiplication_binds_tighter_than_addition():
    assert parse_dax("1 + 2 * 3") == Binary(
        "+", Number("1"), Binary("*", Number("2"), Number("3"))
    )


def test_operators_are_left_associative():
    assert parse_dax("1 - 2 - 3") == Binary(
        "-", Binary("-", Number("1"), Number("2")), Number("3")
    )


def test_exponent_binds_tighter_than_sign():
    # Documented DAX behaviour: -2^2 is -4.
    assert parse_dax("-2^2") == Unary("-", Binary("^", Number("2"), Number("2")))


def test_comparison_concatenation_and_logic_precedence():
    tree = parse_dax('[A] & "x" = "y" && NOT [B] || [C]')
    assert tree == Binary(
        "||",
        Binary(
            "&&",
            Binary("=", Binary("&", Bracket("A"), String("x")), String("y")),
            Unary("NOT", Bracket("B")),
        ),
        Bracket("C"),
    )


def test_not_function_form_is_a_call():
    assert parse_dax("NOT(TRUE())") == Call("NOT", (Call("TRUE", ()),))


def test_in_with_a_table_constructor():
    assert parse_dax("'P'[Color] IN {\"Red\", \"Blue\"}") == Binary(
        "IN", Column("P", "Color"), TableConstructor((String("Red"), String("Blue")))
    )


def test_empty_table_constructor():
    assert parse_dax("{}") == TableConstructor(())


def test_var_return_block():
    tree = parse_dax("VAR a = 1 VAR b = a + 1 RETURN b * 2")
    assert tree == Var(
        (("a", Number("1")), ("b", Binary("+", Name("a"), Number("1")))),
        Binary("*", Name("b"), Number("2")),
    )


def test_comments_and_leading_equals_are_ignored():
    text = "= // line comment\n SUM('S'[A]) -- trailing\n /* block\n comment */"
    assert parse_dax(text) == Call("SUM", (Column("S", "A"),))


def test_numbers_in_all_forms():
    for literal in ("1", "1.5", ".5", "2.", "1e3", "1.2E-4"):
        assert parse_dax(literal) == Number(literal)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   =  ",
        "SUM('S'[A]",
        "SUM('S'[A]))",
        "1 +",
        "VAR a = 1",
        "VAR = 1 RETURN 1",
        "'unterminated",
        "[unterminated",
        "1 # 2",
    ],
)
def test_malformed_input_raises(text):
    with pytest.raises(DaxParseError):
        parse_dax(text)
