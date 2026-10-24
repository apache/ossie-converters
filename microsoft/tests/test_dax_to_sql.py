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

"""Tests for the opt-in DAX-to-OSSIE_SQL measure translator.

The translator must translate exactly the constructs whose SQL equivalent is
exact (six single-column aggregates and composition over them), and refuse
everything else with a reason. The refusals matter as much as the translations:
a wrong translation is a silent wrong number on another engine.
"""

import warnings

import pytest

from ossie_microsoft._dax_to_sql import translate_measure
from ossie_microsoft.semantic_model_to_ossie import build_ossie_document


def _dialects(metric):
    return {d["dialect"]: d["expression"] for d in metric["expression"]["dialects"]}


def _metric(document, name):
    return next(m for m in document["metrics"] if m["name"] == name)


def resolve(table, column):
    """A resolver over a small two-dataset model, mirroring the real converter:
    the DAX table name is the dataset name and the DAX column name is the field
    name. Unknown table/column pairs do not resolve."""
    model = {
        ("Sales", "Amount"): ("Sales", "Amount"),
        ("Sales", "Customer"): ("Sales", "Customer"),
        ("Sales", "Status"): ("Sales", "Status"),
        ("Order Lines", "Qty"): ("Order Lines", "Qty"),  # table name needs quoting
    }
    return model.get((table, column))


@pytest.mark.parametrize(
    "dax, expected",
    [
        ("SUM('Sales'[Amount])", "SUM(Sales.Amount)"),
        ("AVERAGE('Sales'[Amount])", "AVG(Sales.Amount)"),
        ("MIN('Sales'[Amount])", "MIN(Sales.Amount)"),
        ("MAX('Sales'[Amount])", "MAX(Sales.Amount)"),
        ("COUNTA('Sales'[Customer])", "COUNT(Sales.Customer)"),
        ("DISTINCTCOUNTNOBLANK('Sales'[Customer])", "COUNT(DISTINCT Sales.Customer)"),
    ],
)
def test_translates_the_six_aggregates(dax, expected):
    sql, reason = translate_measure(dax, resolve)
    assert reason is None
    assert sql == expected


def test_bare_table_reference_is_accepted():
    sql, reason = translate_measure("SUM(Sales[Amount])", resolve)
    assert reason is None
    assert sql == "SUM(Sales.Amount)"


def test_leading_equals_and_whitespace_are_tolerated():
    sql, reason = translate_measure("  =  SUM( 'Sales'[Amount] )  ", resolve)
    assert reason is None
    assert sql == "SUM(Sales.Amount)"


@pytest.mark.parametrize(
    "dax",
    [
        "COUNT('Sales'[Amount])",            # TRUE/FALSE unsupported in DAX
        "DISTINCTCOUNT('Sales'[Customer])",  # BLANK counted as distinct
        "COUNTROWS('Sales')",                # row count, not single column
        "MEDIAN('Sales'[Amount])",           # later tier
        "SUMX('Sales', 'Sales'[Amount])",    # iterator, two args
        "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Status] = \"done\")",  # filter context
        "DIVIDE(SUM('Sales'[Amount]), COUNTA('Sales'[Customer]), 0)",   # alternate result
        "SUM('Sales'[Amount]) / COUNTA('Sales'[Customer])",             # / operator
        "SUM('Sales'[Amount]) ^ 2",          # exponent, later tier
        "-SUM('Sales'[Amount])",             # negating an expression, later tier
        "IF('Sales'[Amount] > 0, 1, 0)",     # scalar logic
        "'Sales'[Amount]",                   # bare column, not a measure
    ],
)
def test_refuses_everything_outside_the_safe_set(dax):
    sql, reason = translate_measure(dax, resolve)
    assert sql is None
    assert reason  # a non-empty reason is always given


def test_count_and_distinctcount_refusals_name_the_semantic_difference():
    _, count_reason = translate_measure("COUNT('Sales'[Amount])", resolve)
    _, distinct_reason = translate_measure("DISTINCTCOUNT('Sales'[Customer])", resolve)
    assert "TRUE/FALSE" in count_reason
    assert "BLANK" in distinct_reason


def test_unqualified_column_is_refused():
    sql, reason = translate_measure("SUM([Amount])", resolve)
    assert sql is None
    assert "fully qualified" in reason


def test_unresolved_column_is_refused():
    sql, reason = translate_measure("SUM('Ghost'[Nope])", resolve)
    assert sql is None
    assert "does not resolve" in reason


def test_name_needing_quoting_is_refused_in_this_tier():
    # 'Order Lines' resolves, but the dataset name has a space and would need
    # SQL quoting, which this first tier declines rather than risk getting wrong.
    sql, reason = translate_measure("SUM('Order Lines'[Qty])", resolve)
    assert sql is None
    assert "quoting" in reason


def test_empty_measure_is_refused():
    sql, reason = translate_measure("   ", resolve)
    assert sql is None
    assert reason


def test_nested_call_does_not_parse_as_single_aggregate():
    sql, reason = translate_measure("SUM(ROUND('Sales'[Amount], 2))", resolve)
    assert sql is None
    assert reason


# --- Composition: DIVIDE, arithmetic, measure references, VAR ---------------

MEASURES = {
    "Total Sales": "SUM('Sales'[Amount])",
    "Customers": "COUNTA('Sales'[Customer])",
    "Avg Sale": "DIVIDE([Total Sales], [Customers])",
    "Row Count": "COUNTROWS('Sales')",
    "Loop": "[Loop] + 1",
}

BOTH_NULL_GUARD = (
    "CASE WHEN SUM(Sales.Amount) IS NULL AND COUNT(Sales.Customer) IS NULL THEN NULL "
    "ELSE COALESCE(SUM(Sales.Amount), 0) {op} COALESCE(COUNT(Sales.Customer), 0) END"
)


@pytest.mark.parametrize(
    "dax, expected",
    [
        # DIVIDE: BLANK on a zero or BLANK denominator, which NULLIF reproduces.
        (
            "DIVIDE(SUM('Sales'[Amount]), COUNTA('Sales'[Customer]))",
            "SUM(Sales.Amount) / NULLIF(COUNT(Sales.Customer), 0)",
        ),
        # * is exact: BLANK times anything is BLANK, like NULL.
        ("SUM('Sales'[Amount]) * 1.1", "SUM(Sales.Amount) * 1.1"),
        # + and -: BLANK counts as 0 unless both sides are BLANK.
        (
            "SUM('Sales'[Amount]) + COUNTA('Sales'[Customer])",
            BOTH_NULL_GUARD.format(op="+"),
        ),
        (
            "SUM('Sales'[Amount]) - COUNTA('Sales'[Customer])",
            BOTH_NULL_GUARD.format(op="-"),
        ),
        ("SUM('Sales'[Amount]) + 1", "COALESCE(SUM(Sales.Amount), 0) + 1"),
        ("1 - SUM('Sales'[Amount])", "1 - COALESCE(SUM(Sales.Amount), 0)"),
        # / only by a non-zero literal.
        ("SUM('Sales'[Amount]) / 1000", "SUM(Sales.Amount) / 1000"),
        # Parentheses are kept only where precedence needs them.
        ("SUM('Sales'[Amount]) * (2 + 3)", "SUM(Sales.Amount) * (2 + 3)"),
        ("(SUM('Sales'[Amount]) * 2) * 3", "SUM(Sales.Amount) * 2 * 3"),
        ("+SUM('Sales'[Amount]) * -1", "SUM(Sales.Amount) * -1"),
        # Comments and line breaks are ignored.
        (
            "// gross sales\nSUM( 'Sales'[Amount] ) /* in EUR */",
            "SUM(Sales.Amount)",
        ),
    ],
)
def test_translates_composition(dax, expected):
    sql, reason = translate_measure(dax, resolve, MEASURES)
    assert reason is None, reason
    assert sql == expected


def test_measure_references_are_expanded_inline():
    sql, reason = translate_measure("[Avg Sale] * 100", resolve, MEASURES)
    assert reason is None, reason
    assert sql == "SUM(Sales.Amount) / NULLIF(COUNT(Sales.Customer), 0) * 100"


def test_measure_reference_names_are_case_insensitive():
    sql, reason = translate_measure("[total sales]", resolve, MEASURES)
    assert reason is None
    assert sql == "SUM(Sales.Amount)"


def test_var_return_is_substituted():
    dax = """
        VAR sales = SUM('Sales'[Amount])
        VAR customers = COUNTA('Sales'[Customer])
        VAR ratio = DIVIDE(Sales, customers)
        RETURN ratio * 100
    """
    sql, reason = translate_measure(dax, resolve, MEASURES)
    assert reason is None, reason
    assert sql == "SUM(Sales.Amount) / NULLIF(COUNT(Sales.Customer), 0) * 100"


@pytest.mark.parametrize(
    "dax, fragment",
    [
        ("[Row Count] * 2", "referenced measure [Row Count] is not translated: COUNTROWS"),
        ("[Loop]", "references itself"),
        ("[Nope]", "is not a measure of this model"),
        ("VAR a = 1 RETURN b", "'b' is not a variable"),
        ("SUM('Sales'[Amount]) / 0", "the / operator"),
        ("CALCULATE(SUM('Sales'[Amount]), 'Sales'[Status] = \"done\")", "filter-context"),
        ("TOTALYTD(SUM('Sales'[Amount]), 'Date'[Date])", "window measures"),
        ("SUMMARIZE('Sales', 'Sales'[Status])", "returns a table"),
        ("FOO('Sales'[Amount])", "not a DAX function known"),
        ("SUM('Sales'[Amount]", "could not be parsed"),
    ],
)
def test_refusals_carry_a_specific_reason(dax, fragment):
    sql, reason = translate_measure(dax, resolve, MEASURES)
    assert sql is None
    assert fragment in reason


def test_measure_reference_without_the_model_measures_is_refused():
    sql, reason = translate_measure("[Total Sales]", resolve)
    assert sql is None
    assert "is not a measure of this model" in reason


# --- The emitted SQL parses, and keeps DAX's BLANK semantics when run ---------

TRANSLATED = [
    "DIVIDE(SUM('Sales'[Amount]), COUNTA('Sales'[Customer]))",
    "SUM('Sales'[Amount]) + COUNTA('Sales'[Customer])",
    "SUM('Sales'[Amount]) - SUM('Sales'[Amount]) * 2",
    "VAR s = SUM('Sales'[Amount]) RETURN s + 1",
    "[Avg Sale] * 100",
]


@pytest.mark.parametrize("dax", TRANSLATED)
def test_emitted_sql_parses(dax):
    sqlglot = pytest.importorskip("sqlglot")
    sql, reason = translate_measure(dax, resolve, MEASURES)
    assert reason is None, reason
    sqlglot.parse_one(sql)


def _evaluate(sql, rows):
    """Run an emitted aggregate expression over a Sales table holding ``rows``."""
    import sqlite3

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE Sales (Amount REAL, Customer TEXT)")
        connection.executemany("INSERT INTO Sales VALUES (?, ?)", rows)
        return connection.execute(f"SELECT {sql} FROM Sales").fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "dax, rows, dax_result",
    [
        # DAX: BLANK + BLANK is BLANK; SUM over no rows is BLANK.
        ("SUM('Sales'[Amount]) + SUM('Sales'[Amount])", [], None),
        # DAX: BLANK + 5 is 5 (SQL alone would give NULL).
        ("SUM('Sales'[Amount]) + 5", [], 5),
        # DAX: BLANK * 5 is BLANK.
        ("SUM('Sales'[Amount]) * 5", [], None),
        # DAX: DIVIDE by BLANK is BLANK (SQL alone would give NULL or an error).
        ("DIVIDE(5, SUM('Sales'[Amount]))", [], None),
        # DAX: DIVIDE by zero is BLANK.
        ("DIVIDE(SUM('Sales'[Amount]), SUM('Sales'[Amount]))", [(0.0, "a")], None),
        # Ordinary values are unchanged.
        ("DIVIDE(SUM('Sales'[Amount]), COUNTA('Sales'[Customer]))", [(10.0, "a"), (20.0, "b")], 15),
        ("SUM('Sales'[Amount]) - 1", [(10.0, "a")], 9),
    ],
)
def test_emitted_sql_matches_dax_blank_semantics(dax, rows, dax_result):
    sql, reason = translate_measure(dax, resolve, MEASURES)
    assert reason is None, reason
    assert _evaluate(sql, rows) == dax_result


# --- Integration: the opt-in flag wired through build_ossie_document ---------


def _build(bim, translate_dax):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return build_ossie_document(bim, translate_dax=translate_dax)


def test_default_keeps_every_measure_as_dax_only(bim):
    document = _build(bim, translate_dax=False)
    for metric in document["metrics"]:
        assert list(_dialects(metric)) == ["DAX"], metric["name"]


def test_flag_translates_a_single_column_aggregate_and_keeps_dax_fallback(bim):
    document = _build(bim, translate_dax=True)
    dialects = _dialects(_metric(document, "Total Sales"))
    # OSSIE_SQL_2026 is added as the portable rendering ...
    assert dialects["OSSIE_SQL_2026"] == "SUM(Sales.Amount)"
    # ... and the original DAX is preserved as a fallback, so nothing is lost.
    assert dialects["DAX"] == "SUM ( Sales[Amount] )"


def test_flag_refuses_countrows_and_leaves_it_as_dax(bim):
    document = _build(bim, translate_dax=True)
    dialects = _dialects(_metric(document, "Order Count"))
    assert list(dialects) == ["DAX"]


def test_flag_is_order_preserving_portable_first(bim):
    document = _build(bim, translate_dax=True)
    first = _metric(document, "Total Sales")["expression"]["dialects"][0]
    assert first["dialect"] == "OSSIE_SQL_2026"


def _bim_with_measures(bim, measures):
    """A copy of the fixture with extra measures on Sales (the fixture is module-scoped,
    so it is never mutated)."""
    import copy

    model = copy.deepcopy(bim)
    sales = next(t for t in model["model"]["tables"] if t["name"] == "Sales")
    sales["measures"] = list(sales.get("measures") or []) + [
        {"name": name, "expression": expression} for name, expression in measures.items()
    ]
    return model


def test_flag_expands_measure_references_across_the_model(bim):
    model = _bim_with_measures(
        bim,
        {
            "Tax": "SUM(Sales[AmountWithTax]) - [Total Sales]",
            "Tax Rate": "VAR tax = [Tax] RETURN DIVIDE(tax, [Total Sales])",
            "Per Order": "DIVIDE([Total Sales], [Order Count])",
        },
    )
    document = _build(model, translate_dax=True)

    tax = _dialects(_metric(document, "Tax"))
    assert tax["OSSIE_SQL_2026"] == (
        "CASE WHEN SUM(Sales.AmountWithTax) IS NULL AND SUM(Sales.Amount) IS NULL THEN NULL "
        "ELSE COALESCE(SUM(Sales.AmountWithTax), 0) - COALESCE(SUM(Sales.Amount), 0) END"
    )
    assert tax["DAX"] == "SUM(Sales[AmountWithTax]) - [Total Sales]"

    rate = _dialects(_metric(document, "Tax Rate"))
    assert rate["OSSIE_SQL_2026"].endswith(" / NULLIF(SUM(Sales.Amount), 0)")

    # Order Count is COUNTROWS, a later tier, so a measure built on it stays DAX only.
    assert list(_dialects(_metric(document, "Per Order"))) == ["DAX"]


def test_a_literal_that_is_not_a_number_is_a_later_tier():
    sql, reason = translate_measure('"not a number"', resolve, MEASURES)
    assert sql is None
    assert "string expressions are a later tier" in reason


def test_a_referenced_measure_that_does_not_parse_is_refused():
    measures = dict(MEASURES, Broken="SUM('Sales'[Amount]")
    sql, reason = translate_measure("[Broken] * 2", resolve, measures)
    assert sql is None
    assert "referenced measure [Broken] could not be parsed" in reason
