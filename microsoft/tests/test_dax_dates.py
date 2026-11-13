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

"""Tests for the DAX date functions: extraction, DATE, EDATE and EOMONTH.

A date that may be BLANK is refused (DAX reads BLANK as 30 December 1899, SQL gives
NULL), so every translation here starts from a value that can never be BLANK: a DATE
literal, COALESCE with one, or a column declared not nullable.
"""

import copy
import warnings

import pytest
import sqlglot

from ossie_microsoft._dax_to_sql import RowColumn, translate_column, translate_measure
from ossie_microsoft.ossie_to_semantic_model import convert_ossie_to_semantic_model
from ossie_microsoft.semantic_model_to_ossie import build_ossie_document, dump_yaml


def resolve(table, column):
    model = {
        ("Sales", "OrderDate"): ("Sales", "OrderDate", "Date"),
        ("Sales", "Stamp"): ("Sales", "Stamp", "DateTime"),
        ("Sales", "Zoned"): ("Sales", "Zoned", "DateTimeTz"),
        ("Sales", "Clock"): ("Sales", "Clock", "Time"),
        ("Sales", "Amount"): ("Sales", "Amount", "Decimal"),
    }
    return model.get((table, column))


#: An Orders table: a never-BLANK date, a nullable date, a never-BLANK datetime.
COLUMNS = {
    "Placed": RowColumn("placed_on", None, "date", nullable=False),
    "Shipped": RowColumn("shipped_on", None, "date"),
    "Stamp": RowColumn("created_at", None, "datetime", nullable=False),
    "Qty": RowColumn("qty", None, "number", nullable=False),
}


def column(dax):
    return translate_column(dax, "Orders", COLUMNS)


def _parses_everywhere(sql):
    for dialect in (None, "databricks", "snowflake", "bigquery", "postgres", "duckdb"):
        sqlglot.parse_one(sql, read=dialect)


# --- calculated columns: golden translations ----------------------------------


@pytest.mark.parametrize(
    ("dax", "sql"),
    [
        ("YEAR([Placed])", "YEAR(placed_on)"),
        ("QUARTER([Placed])", "QUARTER(placed_on)"),
        ("MONTH('Orders'[Placed])", "MONTH(placed_on)"),
        ("DAY([Placed])", "DAY(placed_on)"),
        ("YEAR([Stamp])", "YEAR(created_at)"),
        ("HOUR([Stamp])", "HOUR(created_at)"),
        ("MINUTE([Stamp])", "MINUTE(created_at)"),
        ("SECOND([Stamp])", "SECOND(created_at)"),
        ("EDATE([Placed], 3)", "DATEADD(month, 3, placed_on)"),
        ("EDATE([Placed], -1)", "DATEADD(month, -1, placed_on)"),
        ("EOMONTH([Placed], 0)",
         "CAST(DATEADD(day, -1, DATEADD(month, 1, DATE_TRUNC('month', placed_on))) AS DATE)"),
        ("EOMONTH([Placed], -2)",
         "CAST(DATEADD(day, -1, DATEADD(month, -1, DATE_TRUNC('month', placed_on))) AS DATE)"),
        ("EOMONTH([Stamp], 1)",
         "CAST(DATEADD(day, -1, DATEADD(month, 2, DATE_TRUNC('month', created_at))) AS DATE)"),
        ("YEAR(COALESCE([Shipped], DATE(2000, 1, 1)))",
         "YEAR(COALESCE(shipped_on, DATE '2000-01-01'))"),
        ("MONTH([Placed]) * 100 + DAY([Placed])",
         "MONTH(placed_on) * 100 + DAY(placed_on)"),
        ('IF(QUARTER([Placed]) = 4, "Q4", "other")',
         "CASE WHEN QUARTER(placed_on) = 4 THEN 'Q4' ELSE 'other' END"),
    ],
)
def test_date_functions_in_calculated_columns(dax, sql):
    assert column(dax) == (sql, None)
    _parses_everywhere(sql)


# --- measures -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dax", "sql"),
    [
        ("DATE(2024, 3, 15)", "DATE '2024-03-15'"),
        ("YEAR(DATE(2024, 3, 15))", "YEAR(DATE '2024-03-15')"),
        ("YEAR(COALESCE(MAX('Sales'[OrderDate]), DATE(1900, 1, 1)))",
         "YEAR(COALESCE(MAX(Sales.OrderDate), DATE '1900-01-01'))"),
        ("EOMONTH(DATE(2024, 1, 31), 1)",
         "CAST(DATEADD(day, -1, DATEADD(month, 2, DATE_TRUNC('month', DATE '2024-01-31'))) "
         "AS DATE)"),
        # A date and a datetime combine as a datetime, so HOUR can read the result.
        ("HOUR(COALESCE(MAX('Sales'[Stamp]), DATE(2000, 1, 1)))",
         "HOUR(COALESCE(MAX(Sales.Stamp), DATE '2000-01-01'))"),
    ],
)
def test_date_functions_in_measures(dax, sql):
    assert translate_measure(dax, resolve) == (sql, None)
    _parses_everywhere(sql)


# --- refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        # BLANK is 30 December 1899 in DAX, NULL in SQL.
        ("YEAR([Shipped])", "date that may be BLANK"),
        ("EOMONTH([Shipped], 0)", "date that may be BLANK"),
        # HOUR of a date-only value is 0 in DAX; the spec's HOUR takes a timestamp.
        ("HOUR([Placed])", "needs a datetime value"),
        # Whether EDATE keeps the time of day is not documented.
        ("EDATE([Stamp], 1)", "needs a date value"),
        ("YEAR([Qty])", "YEAR of a number value"),
        ("EDATE([Placed], [Qty])", "integer literal number of months"),
        ("EDATE([Placed], 1.5)", "integer literal number of months"),
        ("EOMONTH([Placed])", "EOMONTH takes 2 arguments"),
        ("YEAR([Placed], 1)", "YEAR takes 1 argument"),
        ("DATEDIFF([Placed], [Placed], DAY)", "boundaries"),
        ("[Placed] + 1", "later tier"),
    ],
)
def test_date_refusals_in_calculated_columns(dax, reason):
    sql, why = column(dax)
    assert sql is None
    assert reason in why, why


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        # MAX over a date is BLANK on an empty set.
        ("YEAR(MAX('Sales'[OrderDate]))", "date that may be BLANK"),
        ("YEAR(COALESCE(MAX('Sales'[Zoned]), DATE(2000, 1, 1)))", "results of different types"),
        ("MONTH(COALESCE(MAX('Sales'[Clock]), DATE(2000, 1, 1)))", "results of different types"),
        ("YEAR(COALESCE(MAX('Sales'[Amount]), 0))", "YEAR of a number value"),
        # DAX shifts years before 1900 and rolls out-of-range parts over.
        ("DATE(99, 1, 1)", "from year 1900 on"),
        ("DATE(2024, 13, 1)", "valid date"),
        ("DATE(2023, 2, 29)", "valid date"),
        ("DATE(YEAR(DATE(2024, 1, 1)), 1, 1)", "integer literals"),
        ("DATE(2024, 1)", "DATE takes 3 arguments"),
    ],
)
def test_date_refusals_in_measures(dax, reason):
    sql, why = translate_measure(dax, resolve)
    assert sql is None
    assert reason in why, why


# --- import -----------------------------------------------------------------------


def _with_columns(bim, *columns):
    model = copy.deepcopy(bim)
    sales = next(t for t in model["model"]["tables"] if t["name"] == "Sales")
    sales["columns"] = list(sales["columns"]) + list(columns)
    return model


def _dialects(element):
    return {d["dialect"]: d["expression"] for d in element["expression"]["dialects"]}


def _field(document, name):
    sales = next(d for d in document["datasets"] if d["name"] == "Sales")
    return next(f for f in sales["fields"] if f["name"] == name)


_DATE_COLUMNS = (
    {"name": "Booked", "dataType": "dateTime", "formatString": "yyyy-mm-dd",
     "sourceColumn": "booked_on", "isNullable": False},
    {"name": "Logged", "dataType": "dateTime", "formatString": "yyyy-mm-dd hh:nn:ss",
     "sourceColumn": "logged_at", "isNullable": False},
    {"name": "BookedYear", "type": "calculated", "dataType": "int64",
     "expression": "YEAR([Booked])"},
    {"name": "LoggedHour", "type": "calculated", "dataType": "int64",
     "expression": "HOUR([Logged])"},
    {"name": "BookedHour", "type": "calculated", "dataType": "int64",
     "expression": "HOUR([Booked])"},
)


def test_import_reads_date_and_datetime_columns_from_the_format_string(bim):
    # A dateTime with a date-only format string is a date, as on import.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        document = build_ossie_document(_with_columns(bim, *_DATE_COLUMNS), translate_dax=True)
    assert _dialects(_field(document, "BookedYear"))["OSSIE_SQL_2026"] == "YEAR(booked_on)"
    assert _dialects(_field(document, "LoggedHour"))["OSSIE_SQL_2026"] == "HOUR(logged_at)"
    assert list(_dialects(_field(document, "BookedHour"))) == ["DAX"]


def test_round_trip_with_date_translations_is_unchanged(bim):
    # Export prefers DAX, so a translated column goes back as the same calculated column.
    model = _with_columns(bim, *_DATE_COLUMNS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        plain = convert_ossie_to_semantic_model(dump_yaml(build_ossie_document(model)))
        translated = convert_ossie_to_semantic_model(
            dump_yaml(build_ossie_document(model, translate_dax=True))
        )
    assert translated == plain
