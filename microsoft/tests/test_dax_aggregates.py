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

"""Tests for the column aggregates translated by rule: COUNT, DISTINCTCOUNT, MEDIAN,
PERCENTILE.INC and the STDEV / VAR family, and the aggregates refused with a reason."""

import sqlite3

import pytest
import sqlglot

from ossie_microsoft._dax_to_sql import RowColumn, translate_column, translate_measure


def resolve(table, column):
    model = {
        ("Sales", "Amount"): ("Sales", "Amount", "Decimal"),
        ("Sales", "Customer"): ("Sales", "Customer", "String"),
        ("Sales", "OrderDate"): ("Sales", "OrderDate", "Date"),
        ("Sales", "Paid"): ("Sales", "Paid", "Boolean"),
        ("Sales", "Note"): ("Sales", "Note", None),
        ("Order Lines", "Qty"): ("Order Lines", "Qty", "Integer"),  # needs SQL quoting
    }
    return model.get((table, column))


def _parses_everywhere(sql):
    for dialect in (None, "databricks", "snowflake", "bigquery", "postgres", "duckdb"):
        sqlglot.parse_one(sql, read=dialect)


@pytest.mark.parametrize(
    ("dax", "sql"),
    [
        ("COUNT('Sales'[Amount])", "COUNT(Sales.Amount)"),
        ("COUNT('Sales'[Customer])", "COUNT(Sales.Customer)"),
        ("COUNT('Sales'[OrderDate])", "COUNT(Sales.OrderDate)"),
        ("DISTINCTCOUNT('Sales'[Customer])",
         "COUNT(DISTINCT Sales.Customer) "
         "+ MAX(CASE WHEN Sales.Customer IS NULL THEN 1 ELSE 0 END)"),
        ("DISTINCTCOUNT('Sales'[Paid])",
         "COUNT(DISTINCT Sales.Paid) + MAX(CASE WHEN Sales.Paid IS NULL THEN 1 ELSE 0 END)"),
        ("MEDIAN('Sales'[Amount])", "MEDIAN(Sales.Amount)"),
        ("PERCENTILE.INC('Sales'[Amount], 0.9)",
         "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY Sales.Amount)"),
        ("PERCENTILE.INC('Sales'[Amount], 1)",
         "PERCENTILE_CONT(1) WITHIN GROUP (ORDER BY Sales.Amount)"),
        ("STDEV.S('Sales'[Amount])", "STDDEV_SAMP(Sales.Amount)"),
        ("STDEV.P('Sales'[Amount])", "STDDEV_POP(Sales.Amount)"),
        ("VAR.S('Sales'[Amount])", "VAR_SAMP(Sales.Amount)"),
        ("VAR.P('Sales'[Amount])", "VAR_POP(Sales.Amount)"),
        # They compose like the other aggregates: a count is a number that may be BLANK.
        ("DIVIDE(SUM('Sales'[Amount]), COUNT('Sales'[Customer]))",
         "SUM(Sales.Amount) / NULLIF(COUNT(Sales.Customer), 0)"),
        ("IF(MEDIAN('Sales'[Amount]) > 10, 1, 0)",
         "CASE WHEN COALESCE(MEDIAN(Sales.Amount), 0) > 10 THEN 1 ELSE 0 END"),
    ],
)
def test_aggregates_translated_by_rule(dax, sql):
    assert translate_measure(dax, resolve) == (sql, None)
    _parses_everywhere(sql)


@pytest.mark.parametrize(
    ("values", "distinct"),
    [
        (["a", "b", "a"], 2),          # no BLANK: plain COUNT(DISTINCT)
        (["a", None, "b", None], 3),   # BLANK counts as one more distinct value
        ([None, None], 1),             # only BLANK: one distinct value
        ([], None),                    # no rows: BLANK, as in DAX
    ],
)
def test_the_distinctcount_rule_counts_blank_once(values, distinct):
    # Runs the emitted expression on SQLite against the DAX result.
    sql, _ = translate_measure("DISTINCTCOUNT('Sales'[Customer])", resolve)
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE Sales (Customer TEXT)")
    connection.executemany("INSERT INTO Sales VALUES (?)", [(v,) for v in values])
    assert connection.execute(f"SELECT {sql} FROM Sales").fetchone()[0] == distinct


@pytest.mark.parametrize(
    ("dax", "reason"),
    [
        ("COUNT('Sales'[Paid])", "TRUE/FALSE"),
        ("COUNT('Sales'[Note])", "type the model does not give"),
        ("DISTINCTCOUNT('Sales'[Note])", "type the model does not give"),
        ("MEDIAN('Sales'[Customer])", "MEDIAN over a text column"),
        ("STDEV.S('Sales'[OrderDate])", "STDEV.S over a date column"),
        ("MEDIAN('Sales'[Amount], 1)", "one fully qualified column"),
        ("MEDIAN(SUM('Sales'[Amount]))", "one fully qualified column"),
        ("COUNT('Sales'[Missing])", "does not resolve"),
        ("MEDIAN('Order Lines'[Qty])", "needing SQL quoting"),
        ("PERCENTILE.INC('Sales'[Amount], 1.5)", "between 0 and 1"),
        ("PERCENTILE.INC('Sales'[Amount], [Total])", "between 0 and 1"),
        ("PERCENTILE.INC('Sales'[Customer], 0.5)", "over a text column"),
        ("PERCENTILE.INC('Sales'[Amount])", "takes 2 arguments"),
        ("PERCENTILE.EXC('Sales'[Amount], 0.5)", "k * (n + 1)"),
        ("COUNTROWS('Sales')", "reads no dataset"),
        ("COUNTBLANK('Sales'[Amount])", "reads no dataset"),
        ("AVERAGEA('Sales'[Amount])", "text as 0"),
        ("MINA('Sales'[Amount])", "text as 0"),
        ("MAXA('Sales'[Amount])", "text as 0"),
    ],
)
def test_aggregate_refusals(dax, reason):
    sql, why = translate_measure(dax, resolve)
    assert sql is None
    assert reason in why, why


def test_aggregates_are_refused_in_calculated_columns():
    columns = {"Amount": RowColumn("amount", None, "number")}
    sql, why = translate_column("MEDIAN('Orders'[Amount])", "Orders", columns)
    assert sql is None
    assert "ranges over the whole table" in why
