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

"""Tests for the X-iterators (SUMX, AVERAGEX, ...): the row expression is translated in
the row context of the iterated table and aggregated, as DAX does."""

import sqlite3

import pytest
import sqlglot

from ossie_microsoft._dax_to_sql import RowColumn, translate_column, translate_measure


def resolve(table, column):
    model = {
        ("Sales", "Qty"): ("Sales", "Qty", "Integer"),
        ("Sales", "Price"): ("Sales", "Price", "Decimal"),
        ("Sales", "Discount"): ("Sales", "Discount", "Decimal"),
        ("Sales", "Shipped"): ("Sales", "Shipped", "Date"),
        ("Sales", "Customer"): ("Sales", "Customer", "String"),
        ("Sales", "Paid"): ("Sales", "Paid", "Boolean"),
        ("Product", "Cost"): ("Product", "Cost", "Decimal"),
        ("Order Lines", "Qty"): ("Order Lines", "Qty", "Integer"),
    }
    return model.get((table, column))


def _parses_everywhere(sql):
    for dialect in (None, "databricks", "snowflake", "bigquery", "postgres", "duckdb"):
        sqlglot.parse_one(sql, read=dialect)


@pytest.mark.parametrize(
    ("dax", "sql"),
    [
        ("SUMX('Sales', 'Sales'[Qty] * 'Sales'[Price])", "SUM(Sales.Qty * Sales.Price)"),
        ("SUMX(Sales, Sales[Qty])", "SUM(Sales.Qty)"),
        ("AVERAGEX('Sales', 'Sales'[Qty] * 'Sales'[Price])", "AVG(Sales.Qty * Sales.Price)"),
        ("MINX('Sales', 'Sales'[Price])", "MIN(Sales.Price)"),
        ("MAXX('Sales', 'Sales'[Shipped])", "MAX(Sales.Shipped)"),
        ("COUNTX('Sales', 'Sales'[Customer])", "COUNT(Sales.Customer)"),
        ("MEDIANX('Sales', 'Sales'[Price] * 2)", "MEDIAN(Sales.Price * 2)"),
        ("PERCENTILEX.INC('Sales', 'Sales'[Price], 0.25)",
         "PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY Sales.Price)"),
        ("STDEVX.S('Sales', 'Sales'[Price])", "STDDEV_SAMP(Sales.Price)"),
        ("STDEVX.P('Sales', 'Sales'[Price])", "STDDEV_POP(Sales.Price)"),
        ("VARX.S('Sales', 'Sales'[Price])", "VAR_SAMP(Sales.Price)"),
        ("VARX.P('Sales', 'Sales'[Price])", "VAR_POP(Sales.Price)"),
        # Row-level BLANK rules: BLANK - x is -x, so each operand is COALESCEd and two
        # BLANKs stay BLANK (skipped by SUM, as in DAX).
        ("SUMX('Sales', 'Sales'[Price] - 'Sales'[Discount])",
         "SUM(CASE WHEN Sales.Price IS NULL AND Sales.Discount IS NULL THEN NULL "
         "ELSE COALESCE(Sales.Price, 0) - COALESCE(Sales.Discount, 0) END)"),
        ("SUMX('Sales', IF('Sales'[Qty] > 10, 'Sales'[Price], 0))",
         "SUM(CASE WHEN COALESCE(Sales.Qty, 0) > 10 THEN Sales.Price ELSE 0 END)"),
        # An iterator composes like any aggregate.
        ("DIVIDE(SUMX('Sales', 'Sales'[Qty] * 'Sales'[Price]), SUM('Sales'[Qty]))",
         "SUM(Sales.Qty * Sales.Price) / NULLIF(SUM(Sales.Qty), 0)"),
    ],
)
def test_iterators(dax, sql):
    assert translate_measure(dax, resolve) == (sql, None)
    _parses_everywhere(sql)


def test_sumx_follows_dax_blank_rules_on_sqlite():
    # Price - Discount per row: BLANK - 2 is -2, 5 - BLANK is 5, BLANK - BLANK is skipped.
    sql, _ = translate_measure("SUMX('Sales', 'Sales'[Price] - 'Sales'[Discount])", resolve)
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE Sales (Price REAL, Discount REAL)")
    connection.executemany(
        "INSERT INTO Sales VALUES (?, ?)", [(10, 1), (None, 2), (5, None), (None, None)]
    )
    assert connection.execute(f"SELECT {sql} FROM Sales").fetchone()[0] == 9 - 2 + 5
    connection.execute("DELETE FROM Sales")
    assert connection.execute(f"SELECT {sql} FROM Sales").fetchone()[0] is None  # BLANK


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        ("SUMX('Sales', 'Product'[Cost])", "needs RELATED"),
        ("SUMX('Sales', [Total])", "context transition"),
        ("SUMX('Sales', SUM('Sales'[Qty]))", "nested aggregation"),
        ("SUMX('Sales', SUMX('Sales', 'Sales'[Qty]))", "nested aggregation"),
        ("SUMX('Sales', 1)", "reads no column"),
        ("SUMX(FILTER('Sales', 'Sales'[Qty] > 1), 'Sales'[Qty])", "table named directly"),
        ("SUMX('Sales', 'Sales'[Customer])", "SUMX of a text row expression"),
        ("COUNTX('Sales', 'Sales'[Paid])", "COUNTX of a boolean row expression"),
        ("SUMX('Sales')", "takes 2 arguments"),
        ("PERCENTILEX.INC('Sales', 'Sales'[Price], 2)", "between 0 and 1"),
        ("SUMX('Sales', 'Sales'[Missing])", "does not resolve"),
        ("SUMX('Order Lines', 'Order Lines'[Qty])", "needing SQL quoting"),
        ("PERCENTILEX.EXC('Sales', 'Sales'[Price], 0.5)", "PERCENTILEX.EXC"),
    ],
)
def test_iterator_refusals(dax, reason):
    sql, why = translate_measure(dax, resolve)
    assert sql is None
    assert reason in why, why


def test_a_variable_is_not_a_table():
    sql, why = translate_measure("VAR t = 1 RETURN SUMX(t, 'Sales'[Qty])", resolve)
    assert sql is None
    assert "table named directly" in why


def test_iterators_are_refused_in_calculated_columns():
    columns = {"Qty": RowColumn("qty", None, "number")}
    sql, why = translate_column("SUMX('Orders', 'Orders'[Qty])", "Orders", columns)
    assert sql is None
    assert "ranges over a whole table" in why
