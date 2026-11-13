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
        ('SUBSTITUTE([First], "a", "b")', "REPLACE(first_name, 'a', 'b')"),
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
# SUBSTITUTE replaces every occurrence; UPPER / LOWER; & concatenates.
@pytest.mark.parametrize(
    ("dax", "value", "expected"),
    [
        ("MID([First], 2, 3)", "abcdef", "bcd"),
        ("MID([First], 9, 3)", "abcdef", ""),
        ("LEN([First])", "abc", 3),
        ('SUBSTITUTE([First], "a", "X")', "banana", "bXnXnX"),
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
        ('SUBSTITUTE([First], "a", "b", 2)', "instance_num"),
        ('SUBSTITUTE([First], "", "b")', "non-empty literal to find"),
        ("SUM(Customers[Visits])", "SUM in a calculated column ranges over the whole table"),
        ("Orders[Amount]", "column of another table"),
        ("[Joined]", "has a type that is a later tier"),
        ("[Odd Name]", "needs SQL quoting"),
        ("[Nope]", "[Nope] is not a column of 'Customers'"),
        ("TRIM([First])", "DAX TRIM also collapses inner runs of spaces"),
        ('EXACT([First], "a")', "DAX EXACT is case-sensitive"),
        ("VALUE([First])", "DAX VALUE raises an error"),
        ('FIND("a", [First])', "DAX FIND raises an error"),
        ('SEARCH("a", [First])', "DAX SEARCH"),
        ('CONTAINSSTRING([First], "a")', "DAX CONTAINSSTRING"),
        ("LEFT([First], 1, 2)", "LEFT takes 1 or 2 arguments"),
        ("MID([First], 1)", "MID takes 3 arguments"),
        ("LEN([First], 1)", "LEN takes 1 argument"),
        ("UPPER()", "UPPER takes 1 argument"),
        ('SUBSTITUTE([First], "a")', "SUBSTITUTE takes 3 arguments"),
        ('CONCATENATE([First])', "CONCATENATE takes 2 arguments"),
        ("SUM(", "could not be parsed"),
    ],
)
def test_calculated_column_refusals(dax, reason):
    sql, why = column(dax)
    assert sql is None
    assert reason in why, why


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
    field = _field(_build(bim, translate_dax=True), "Sales", "AmountWithTax")
    assert field["expression"]["dialects"][0] == {
        "dialect": "OSSIE_SQL_2026",
        "expression": "amount * 1.2",  # Amount reads the source column `amount`
    }
    assert _dialects(field)["DAX"] == "Sales[Amount] * 1.2"


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
