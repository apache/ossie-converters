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


"""Tests for translating DAX calculated columns (row context) and the text functions."""

import copy
import sqlite3
import warnings

import pytest
import sqlglot

from ossie_microsoft._dax_to_sql import RowColumn, translate_column, translate_measure
from ossie_microsoft.ossie_to_semantic_model import convert_ossie_to_semantic_model
from ossie_microsoft.semantic_model_to_ossie import build_ossie_document, dump_yaml

#: A Customers table: two text columns (one never BLANK), a number, a date, two
#: calculated columns, one of them built on the other.
COLUMNS = {
    "First": RowColumn("first_name", None, "text", nullable=False),
    "Last": RowColumn("last_name", None, "text"),
    "Visits": RowColumn("visits", None, "number"),
    "Joined": RowColumn("joined", None, None, nullable=False),
    "Odd Name": RowColumn("odd name", None, "text", nullable=False),
    "Full": RowColumn(None, '[First] & " " & COALESCE([Last], "")', None),
    "Initials": RowColumn(None, "LEFT([Full], 1)", None),
}


def column(dax, columns=COLUMNS):
    return translate_column(dax, "Customers", columns)


# --- golden translations ------------------------------------------------------


@pytest.mark.parametrize(
    ("dax", "sql"),
    [
        ("LEFT([First], 2)", "LEFT(first_name, 2)"),
        ("LEFT([First])", "LEFT(first_name, 1)"),
        ("RIGHT('Customers'[First], 3)", "RIGHT(first_name, 3)"),
        ("MID([First], 2, 3)", "SUBSTRING(first_name, 2, 3)"),
        ("LEN([First])", "LENGTH(first_name)"),
        ("UPPER([First])", "UPPER(first_name)"),
        ("LOWER(Customers[First])", "LOWER(first_name)"),
        ('CONCATENATE([First], "!")', "CONCAT(first_name, '!')"),
        ('[First] & "-" & [First]', "CONCAT(CONCAT(first_name, '-'), first_name)"),
        ('LEFT(COALESCE([Last], ""), 1)', "LEFT(COALESCE(last_name, ''), 1)"),
        ('IF(LEN([First]) > 3, "long", "short")',
         "CASE WHEN LENGTH(first_name) > 3 THEN 'long' ELSE 'short' END"),
        ("[Visits] * 2", "visits * 2"),
        ("[Visits]", "visits"),
    ],
)
def test_calculated_column_translations(dax, sql):
    assert column(dax) == (sql, None)
    for dialect in (None, "databricks", "snowflake", "bigquery", "postgres", "duckdb"):
        sqlglot.parse_one(sql, read=dialect)


def test_a_referenced_calculated_column_is_expanded_inline():
    sql, reason = column("[Initials]")
    assert reason is None
    assert sql == "LEFT(CONCAT(CONCAT(first_name, ' '), COALESCE(last_name, '')), 1)"


def test_a_column_reads_its_source_column_not_the_field_name():
    # The spec's field expressions refer to source columns, not to other fields.
    columns = {"Name": RowColumn("cust_nm", None, "text", nullable=False)}
    assert column("UPPER([Name])", columns) == ("UPPER(cust_nm)", None)


def test_text_functions_also_work_in_a_measure_on_literals():
    assert translate_measure('UPPER("abc")', lambda *_: None) == ("UPPER('abc')", None)


# --- the emitted SQL matches DAX's documented results -------------------------

# DAX: MID("abcdef", 2, 3) = "bcd"; MID past the end = ""; LEN("abc") = 3;
# UPPER / LOWER; & concatenates.
@pytest.mark.parametrize(
    ("dax", "value", "expected"),
    [
        ("MID([First], 2, 3)", "abcdef", "bcd"),
        ("MID([First], 9, 3)", "abcdef", ""),
        ("LEN([First])", "abc", 3),
        ("UPPER([First])", "MixEd", "MIXED"),
        ("LOWER([First])", "MixEd", "mixed"),
        ('[First] & "!"', "hi", "hi!"),
    ],
)
def test_emitted_sql_matches_dax_results(dax, value, expected):
    sql, reason = column(dax)
    assert reason is None, reason
    connection = sqlite3.connect(":memory:")
    try:
        result = connection.execute(f"SELECT {sql} FROM (SELECT ? AS first_name)", (value,))
        assert result.fetchone()[0] == expected
    finally:
        connection.close()


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        ("LEFT([Last], 2)", "LEFT of a value that may be BLANK"),
        ('[First] & [Last]', "& of a value that may be BLANK"),
        ("LEFT([Visits], 2)", "LEFT of a number value is a later tier"),
        ("LEFT([First], -1)", "integer literal counts and positions (at least 0)"),
        ("LEFT([First], [Visits])", "integer literal counts and positions"),
        ("MID([First], 0, 2)", "(at least 1)"),
        ("LEFT([First], 1.5)", "integer literal counts"),
        ("SUM(Customers[Visits])", "SUM in a calculated column ranges over the whole table"),
        ("Orders[Amount]", "column of another table"),
        ("[Joined]", "has a type that is a later tier"),
        ("[Odd Name]", "needs SQL quoting"),
        ("[Nope]", "[Nope] is not a column of 'Customers'"),
        ("TRIM([First])", "DAX TRIM also collapses inner runs of spaces"),
        ('EXACT([First], "a")', "DAX EXACT is case-sensitive"),
        ('SUBSTITUTE([First], "a", "b")', "DAX SUBSTITUTE is case-sensitive"),
        ("VALUE([First])", "DAX VALUE raises an error"),
        ('FIND("a", [First])', "DAX FIND raises an error"),
        ('SEARCH("a", [First])', "DAX SEARCH"),
        ('CONTAINSSTRING([First], "a")', "DAX CONTAINSSTRING"),
        ("LEFT([First], 1, 2)", "LEFT takes 1 or 2 arguments"),
        ("MID([First], 1)", "MID takes 3 arguments"),
        ("LEN([First], 1)", "LEN takes 1 argument"),
        ("UPPER()", "UPPER takes 1 argument"),
        ('CONCATENATE([First])', "CONCATENATE takes 2 arguments"),
        ("SUM(", "could not be parsed"),
    ],
)
def test_calculated_column_refusals(dax, reason):
    sql, why = column(dax)
    assert sql is None
    assert reason in why, why


# --- the declared data type ---------------------------------------------------------

# DAX stores a calculated column's value converted to the column's declared data type,
# so an expression of another kind is not the column's value.
@pytest.mark.parametrize(
    ("dax", "declared", "kind"),
    [
        ("[Visits]", "text", "number"),
        ("[First]", "number", "text"),
        ("[Visits] > 3", "number", "boolean"),
        ("[Visits]", "dateTime", "number"),
    ],
)
def test_an_expression_of_another_kind_than_the_declared_type_is_refused(dax, declared, kind):
    sql, reason = translate_column(dax, "Customers", COLUMNS, declared)
    assert sql is None
    assert f"the column is declared as {declared} and its expression is a {kind} value" in reason


@pytest.mark.parametrize(
    ("dax", "declared", "sql"),
    [
        ("[Visits] * 2", "number", "visits * 2"),
        ("UPPER([First])", "text", "UPPER(first_name)"),
        ("[Visits] > 3", "boolean", "COALESCE(visits, 0) > 3"),
        ("[Visits]", None, "visits"),  # no declared type: the type is the expression's
        ("BLANK()", "text", "NULL"),  # BLANK fits every type
    ],
)
def test_an_expression_of_the_declared_kind_is_translated(dax, declared, sql):
    assert translate_column(dax, "Customers", COLUMNS, declared) == (sql, None)


def test_a_referenced_column_of_another_kind_than_its_declared_type_is_refused():
    columns = dict(COLUMNS, VisitsText=RowColumn(None, "[Visits]", "text"))
    sql, reason = column("UPPER([VisitsText])", columns)
    assert sql is None
    assert reason == (
        "referenced column [VisitsText] is not translated: column [VisitsText] is declared "
        "as text and its expression is a number value: DAX converts the value to the "
        "declared type, which is not translated"
    )


def test_a_measure_reference_in_a_calculated_column_is_refused():
    sql, reason = column("[Total Sales]")
    assert sql is None
    assert "is not a column of 'Customers'" in reason
    assert "context transition" in reason


def test_a_very_long_column_chain_is_refused_not_fatal():
    # Each level only references the previous one, so the SQL stays small and only the
    # recursion depth grows.
    columns = {"C0": RowColumn("first_name", None, "text", nullable=False)}
    for level in range(1, 3000):
        columns[f"C{level}"] = RowColumn(None, f"[C{level - 1}]", None)
    sql, reason = column("[C2999]", columns)
    assert sql is None
    assert "too deeply nested" in reason


def test_a_calculated_column_cycle_is_refused():
    columns = {
        "A": RowColumn(None, "[B]", None),
        "B": RowColumn(None, "[A]", None),
    }
    sql, reason = column("[A]", columns)
    assert sql is None
    assert "references itself" in reason


def test_a_referenced_column_that_does_not_parse_is_refused():
    columns = dict(COLUMNS, Broken=RowColumn(None, "LEFT([First]", None))
    sql, reason = column("[Broken]", columns)
    assert sql is None
    assert reason.startswith("referenced column [Broken] could not be parsed")


def test_a_refusal_in_a_column_chain_names_only_the_failing_column():
    columns = {"C0": RowColumn(None, "LEFT([Last], 1)", None)}
    for level in range(1, 6):
        columns[f"C{level}"] = RowColumn(None, f"[C{level - 1}]", None)
    sql, reason = column("[C5]", dict(COLUMNS, **columns))
    assert sql is None
    assert reason.count("referenced column [") == 1, reason
    assert reason.startswith("referenced column [C0] is not translated")


def test_a_long_column_chain_is_bounded_in_size():
    # Each level concatenates the previous column with itself: the SQL doubles per level.
    columns = {"C0": RowColumn("first_name", None, "text", nullable=False)}
    for level in range(1, 20):
        columns[f"C{level}"] = RowColumn(None, f"[C{level - 1}] & [C{level - 1}]", None)
    sql, reason = column("[C19]", columns)
    assert sql is None
    assert "too large to translate exactly" in reason


def test_deep_nesting_in_a_calculated_column_is_refused_not_fatal():
    sql, reason = column("(" * 2000 + "[First]" + ")" * 2000)
    assert sql is None
    assert "too deeply nested" in reason


# --- wired through the import -----------------------------------------------------


def _build(bim, translate_dax):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return build_ossie_document(bim, translate_dax=translate_dax)


def _field(document, dataset, name):
    table = next(d for d in document["datasets"] if d["name"] == dataset)
    return next(f for f in table["fields"] if f["name"] == name)


def _dialects(element):
    return {d["dialect"]: d["expression"] for d in element["expression"]["dialects"]}


def test_default_keeps_calculated_columns_as_dax_only(bim):
    field = _field(_build(bim, translate_dax=False), "Sales", "AmountWithTax")
    assert list(_dialects(field)) == ["DAX"]


def test_flag_translates_a_calculated_column_and_keeps_the_dax(bim):
    model = _with_columns(
        bim,
        {"name": "AmountGross", "type": "calculated", "dataType": "double",
         "expression": "Sales[Amount] * 1.2"},
    )
    field = _field(build_ossie_document(model, translate_dax=True), "Sales", "AmountGross")
    assert field["expression"]["dialects"][0] == {
        "dialect": "OSSIE_SQL_2026",
        "expression": "amount * 1.2",  # Amount reads the source column `amount`
    }
    assert _dialects(field)["DAX"] == "Sales[Amount] * 1.2"


def test_flag_keeps_the_fixture_decimal_calculated_column_as_dax(bim):
    # The fixture declares AmountWithTax as `decimal`: DAX stores amount * 1.2 as a fixed
    # decimal, which the translation would not, so the column stays DAX with the reason.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document = build_ossie_document(bim, translate_dax=True)
    field = _field(document, "Sales", "AmountWithTax")
    assert _dialects(field) == {"DAX": "Sales[Amount] * 1.2"}
    assert any(
        "AmountWithTax'" in str(w.message)
        and "is declared as a fixed decimal and its expression is not known to be one"
        in str(w.message)
        for w in caught
    ), [str(w.message) for w in caught]


def _with_columns(bim, *columns):
    model = copy.deepcopy(bim)
    sales = next(t for t in model["model"]["tables"] if t["name"] == "Sales")
    sales["columns"] = list(sales["columns"]) + list(columns)
    return model


def test_is_nullable_false_is_the_never_blank_precondition(bim):
    model = _with_columns(
        bim,
        {"name": "Code", "dataType": "string", "sourceColumn": "code", "isNullable": False},
        {"name": "Note", "dataType": "string", "sourceColumn": "note"},
        {"name": "CodeStart", "type": "calculated", "dataType": "string",
         "expression": "LEFT([Code], 2)"},
        {"name": "NoteStart", "type": "calculated", "dataType": "string",
         "expression": "LEFT([Note], 2)"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document = build_ossie_document(model, translate_dax=True)
    assert _dialects(_field(document, "Sales", "CodeStart"))["OSSIE_SQL_2026"] == "LEFT(code, 2)"
    assert list(_dialects(_field(document, "Sales", "NoteStart"))) == ["DAX"]
    messages = [str(w.message) for w in caught]
    assert any(
        "NoteStart" in m and "DAX calculated column kept as-is" in m and "may be BLANK" in m
        for m in messages
    ), messages


def test_a_declared_type_the_expression_does_not_have_keeps_the_dax(bim):
    # A column declared as text over a number: DAX converts the number to text.
    model = _with_columns(
        bim,
        {"name": "AmountText", "type": "calculated", "dataType": "string",
         "expression": "[Amount]"},
        {"name": "AmountTextAgain", "type": "calculated", "dataType": "string",
         "expression": "[AmountText]"},
        {"name": "AmountDay", "type": "calculated", "dataType": "dateTime",
         "expression": "[Amount]"},
        {"name": "AmountTwice", "type": "calculated", "dataType": "double",
         "expression": "[Amount] * 2"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document = build_ossie_document(model, translate_dax=True)
    for name in ("AmountText", "AmountTextAgain", "AmountDay"):
        assert list(_dialects(_field(document, "Sales", name))) == ["DAX"], name
    assert _dialects(_field(document, "Sales", "AmountTwice"))["OSSIE_SQL_2026"] == "amount * 2"
    messages = [str(w.message) for w in caught]
    assert any(
        "AmountText'" in m and "is declared as text and its expression is a number value" in m
        for m in messages
    ), messages
    assert any(
        "AmountDay" in m and "and its expression is a number value" in m for m in messages
    ), messages


def test_int64_and_decimal_calculated_columns_keep_the_dax(bim):
    # DAX stores [Amount] / 3 in an int64 column as a whole number and in a decimal
    # column with four decimals; amount / 3 is neither. Amount is a decimal column, so
    # [Amount] * 2 is a fixed decimal, not a whole number. A double column is not converted.
    model = _with_columns(
        bim,
        {"name": "AmountThird", "type": "calculated", "dataType": "int64",
         "expression": "[Amount] / 3"},
        {"name": "AmountTwiceWhole", "type": "calculated", "dataType": "int64",
         "expression": "[Amount] * 2"},
        {"name": "AmountThirdFixed", "type": "calculated", "dataType": "decimal",
         "expression": "[Amount] / 3"},
        {"name": "AmountThirdAgain", "type": "calculated", "dataType": "double",
         "expression": "[AmountThird]"},
        {"name": "AmountThirdFloat", "type": "calculated", "dataType": "double",
         "expression": "[Amount] / 3"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document = build_ossie_document(model, translate_dax=True)
    for name in ("AmountThird", "AmountTwiceWhole", "AmountThirdFixed", "AmountThirdAgain"):
        assert list(_dialects(_field(document, "Sales", name))) == ["DAX"], name
    third = _dialects(_field(document, "Sales", "AmountThirdFloat"))
    assert third["OSSIE_SQL_2026"] == "amount / 3"
    messages = [str(w.message) for w in caught]
    for name, declared in (
        ("AmountThird'", "whole number"),
        ("AmountTwiceWhole'", "whole number"),
        ("AmountThirdFixed'", "fixed decimal"),
    ):
        assert any(
            name in m
            and f"is declared as a {declared} and its expression is not known to be one" in m
            for m in messages
        ), (name, messages)
    # The reference names the int64 column as the reason.
    assert any(
        "AmountThirdAgain'" in m and "column [AmountThird] is declared as a whole number" in m
        for m in messages
    ), messages


@pytest.mark.parametrize("data_type", ["automatic", "unknown", None])
def test_a_calculated_column_without_a_resolved_type_is_translated(bim, data_type):
    # `automatic` and `unknown` declare nothing, like a missing dataType: there is no
    # type to convert to, so the column's type is its expression's.
    declared = {} if data_type is None else {"dataType": data_type}
    model = _with_columns(
        bim,
        {"name": "AmountTwice", "type": "calculated", "expression": "[Amount] * 2", **declared},
        {"name": "AmountFour", "type": "calculated", "dataType": "double",
         "expression": "[AmountTwice] * 2"},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        document = build_ossie_document(model, translate_dax=True)
    assert _dialects(_field(document, "Sales", "AmountTwice"))["OSSIE_SQL_2026"] == "amount * 2"
    # The untyped column is expanded inline where a typed column refers to it.
    assert _dialects(_field(document, "Sales", "AmountFour"))["OSSIE_SQL_2026"] == "amount * 2 * 2"


def test_round_trip_with_translation_is_unchanged(bim):
    # Export prefers DAX, so the translated column goes back as the same calculated column.
    model = _with_columns(
        bim,
        {"name": "Code", "dataType": "string", "sourceColumn": "code", "isNullable": False},
        {"name": "CodeStart", "type": "calculated", "dataType": "string",
         "expression": "LEFT([Code], 2)"},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        plain = convert_ossie_to_semantic_model(dump_yaml(build_ossie_document(model)))
        translated = convert_ossie_to_semantic_model(
            dump_yaml(build_ossie_document(model, translate_dax=True))
        )
    assert translated == plain


# --- whole numbers and fixed decimals -------------------------------------------------

# DAX types Integer and Currency ("fixed decimal") apart from other numbers. A column
# declared int64 or decimal is translated only when its expression is of that type
# already, so the conversion DAX applies on storing the value changes nothing.
NUMBERS = {
    "Qty": RowColumn("qty", None, "number", number="whole number"),
    "Packs": RowColumn("packs", None, "number", nullable=False, number="whole number"),
    "Price": RowColumn("price", None, "number", number="fixed decimal"),
    "Fee": RowColumn("fee", None, "number", number="fixed decimal"),
    "Weight": RowColumn("weight", None, "number"),
    "Code": RowColumn("code", None, "text", nullable=False),
    "Placed": RowColumn("placed_on", None, "date", nullable=False),
    "Twice": RowColumn(None, "[Packs] * 2", "number", number="whole number"),
    "TwiceAsDouble": RowColumn(None, "[Packs] * 2", "number"),
    "TwiceAsFixed": RowColumn(None, "[Packs] * 2", "number", number="fixed decimal"),
}


def typed(dax, number):
    return translate_column(dax, "Orders", NUMBERS, "number", number)


@pytest.mark.parametrize(
    ("dax", "sql"),
    [
        ("7", "7"),
        ("-7", "-7"),
        ("[Packs]", "packs"),
        ("[Packs] * 2", "packs * 2"),
        ("[Packs] * [Packs] - 1", "packs * packs - 1"),
        ("[Qty] + 1", "COALESCE(qty, 0) + 1"),
        ("COALESCE([Qty], 0)", "COALESCE(qty, 0)"),
        ("IF([Packs] > 3, [Packs], 0)", "CASE WHEN packs > 3 THEN packs ELSE 0 END"),
        ("SWITCH(TRUE(), [Packs] > 3, 1, 0)", "CASE WHEN packs > 3 THEN 1 ELSE 0 END"),
        ("VAR n = [Packs] RETURN n * n", "packs * packs"),
        ("LEN([Code])", "LENGTH(code)"),
        ("YEAR([Placed])", "YEAR(placed_on)"),
        ("YEAR([Placed]) * 100 + MONTH([Placed])", "YEAR(placed_on) * 100 + MONTH(placed_on)"),
        ("[Twice] + 1", "packs * 2 + 1"),
        ("BLANK()", "NULL"),
        ("[Packs] + BLANK()", "packs + COALESCE(NULL, 0)"),  # BLANK counts as 0
    ],
)
def test_a_whole_number_expression_is_translated_in_an_int64_column(dax, sql):
    assert typed(dax, "whole number") == (sql, None)


@pytest.mark.parametrize(
    "dax",
    [
        "[Packs] / 2",             # Integer / Integer is a floating-point number
        "DIVIDE([Packs], 2)",
        "[Packs] * 1.5",
        "[Packs] * 2.0",           # a literal with a decimal point is not a whole number
        "[Packs] * 1e3",
        "[Packs] + [Weight]",      # a double column
        "[Packs] * [Price]",       # a fixed decimal is not a whole number
        "ABS([Packs])",            # the math functions are not typed
        "IF([Packs] > 3, [Packs], [Price])",
        "[Packs] * BLANK()",       # a product with BLANK is not typed
        "[TwiceAsDouble]",         # held as a double, whatever its expression is
        "[TwiceAsFixed]",
    ],
)
def test_any_other_number_is_refused_in_an_int64_column(dax):
    sql, reason = typed(dax, "whole number")
    assert sql is None
    assert (
        "the column is declared as a whole number and its expression is not known to be one"
        in reason
    ), reason


@pytest.mark.parametrize(
    ("dax", "sql"),
    [
        ("[Price]", "price"),
        ("[Price] * [Packs]", "price * packs"),
        ("[Packs] * [Price]", "packs * price"),
        ("[Price] * 2", "price * 2"),
        (
            "[Price] + [Fee]",
            "CASE WHEN price IS NULL AND fee IS NULL THEN NULL "
            "ELSE COALESCE(price, 0) + COALESCE(fee, 0) END",
        ),
        ("[Price] - 1", "COALESCE(price, 0) - 1"),
        ("COALESCE([Price], 0)", "COALESCE(price, 0)"),
        # Either branch is a fixed decimal without a change of value.
        ("IF([Packs] > 3, [Price], [Packs])", "CASE WHEN packs > 3 THEN price ELSE packs END"),
        # A whole number converts to a fixed decimal without a change of value.
        ("[Packs] * 2", "packs * 2"),
        ("[TwiceAsFixed] + [Price]", "packs * 2 + COALESCE(price, 0)"),
    ],
)
def test_a_fixed_decimal_expression_is_translated_in_a_decimal_column(dax, sql):
    assert typed(dax, "fixed decimal") == (sql, None)


@pytest.mark.parametrize(
    "dax",
    [
        "[Price] * 1.2",           # Currency * Real is rounded to four decimal places
        "[Price] * [Fee]",         # Currency * Currency is a floating-point number
        "[Price] / 3",             # Currency / Integer is rounded to four decimal places
        "[Packs] / 3",
        "DIVIDE([Price], [Packs])",
        "[Price] + [Weight]",
        "[Price] + 0.5",
        "IF([Packs] > 3, [Price], [Weight])",
    ],
)
def test_any_other_number_is_refused_in_a_decimal_column(dax):
    sql, reason = typed(dax, "fixed decimal")
    assert sql is None
    assert (
        "the column is declared as a fixed decimal and its expression is not known to be one"
        in reason
    ), reason


@pytest.mark.parametrize(
    "dax", ["[Packs] / 2", "[Packs] * 1.5", "[Price] * 1.2", "[Packs] * 2", "[Twice]"]
)
def test_a_double_column_takes_every_number_as_before(dax):
    sql, reason = translate_column(dax, "Orders", NUMBERS, "number")
    assert sql is not None, reason


def test_a_referenced_int64_column_with_another_number_names_the_column():
    columns = dict(NUMBERS, Half=RowColumn(None, "[Packs] / 2", "number", number="whole number"))
    sql, reason = translate_column("[Half] + 1", "Orders", columns, "number")
    assert sql is None
    assert "referenced column [Half] is not translated" in reason
    assert "column [Half] is declared as a whole number" in reason


def test_int64_and_decimal_calculated_columns_are_translated_when_exact(bim):
    # Sales[Amount] is `decimal` in the fixture and its key columns are `int64`.
    model = _with_columns(
        bim,
        {"name": "AmountTwice", "type": "calculated", "dataType": "decimal",
         "expression": "[Amount] * 2"},
        {"name": "AmountTwiceAgain", "type": "calculated", "dataType": "decimal",
         "expression": "[AmountTwice] + [Amount]"},
        {"name": "Seven", "type": "calculated", "dataType": "int64", "expression": "3 + 4"},
        {"name": "SevenFixed", "type": "calculated", "dataType": "decimal",
         "expression": "[Seven]"},
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        document = build_ossie_document(model, translate_dax=True)
    expected = {
        "AmountTwice": "amount * 2",
        "AmountTwiceAgain": "CASE WHEN amount * 2 IS NULL AND amount IS NULL THEN NULL "
        "ELSE COALESCE(amount * 2, 0) + COALESCE(amount, 0) END",
        "Seven": "3 + 4",
        "SevenFixed": "3 + 4",
    }
    for name, sql in expected.items():
        dialects = _dialects(_field(document, "Sales", name))
        assert dialects.get("OSSIE_SQL_2026") == sql, (name, dialects)
        assert "DAX" in dialects, name
    kept = [str(w.message) for w in caught if "kept as-is" in str(w.message)]
    assert not any(name + "'" in m for name in expected for m in kept), kept
