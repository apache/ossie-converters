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


def test_a_text_literal_is_translated_with_quotes_escaped():
    sql, reason = translate_measure('"it\'s ""quoted"""', resolve, MEASURES)
    assert reason is None, reason
    assert sql == "'it''s \"quoted\"'"


def test_a_referenced_measure_that_does_not_parse_is_refused():
    measures = dict(MEASURES, Broken="SUM('Sales'[Amount]")
    sql, reason = translate_measure("[Broken] * 2", resolve, measures)
    assert sql is None
    assert "referenced measure [Broken] could not be parsed" in reason


# --- Logic and conditionals (PR3) --------------------------------------------

LOGIC = dict(MEASURES, Margin="[Total Sales] - [Customers]")


@pytest.mark.parametrize(
    "dax, expected",
    [
        ("BLANK()", "NULL"),
        ("TRUE()", "TRUE"),
        ("COALESCE([Total Sales], 0)", "COALESCE(SUM(Sales.Amount), 0)"),
        # Comparisons coerce a BLANK number to 0, as DAX does.
        ("IF([Total Sales] > 1000, 1, 0)",
         "CASE WHEN COALESCE(SUM(Sales.Amount), 0) > 1000 THEN 1 ELSE 0 END"),
        # IF without a value if false returns BLANK.
        ('IF([Total Sales] > 1000, "High")',
         "CASE WHEN COALESCE(SUM(Sales.Amount), 0) > 1000 THEN 'High' END"),
        # A numeric test is TRUE when non-zero; BLANK is FALSE.
        ("IF([Total Sales], 1, 0)",
         "CASE WHEN COALESCE(SUM(Sales.Amount), 0) <> 0 THEN 1 ELSE 0 END"),
        ("IF(ISBLANK([Total Sales]), 0, [Total Sales])",
         "CASE WHEN SUM(Sales.Amount) IS NULL THEN 0 ELSE SUM(Sales.Amount) END"),
        ("IF.EAGER([Total Sales] = 0, BLANK(), 1)",
         "CASE WHEN COALESCE(SUM(Sales.Amount), 0) = 0 THEN NULL ELSE 1 END"),
        ('SWITCH(TRUE, [Total Sales] > 100, "A", [Total Sales] > 10, "B", "C")',
         "CASE WHEN COALESCE(SUM(Sales.Amount), 0) > 100 THEN 'A' "
         "WHEN COALESCE(SUM(Sales.Amount), 0) > 10 THEN 'B' ELSE 'C' END"),
        ("SWITCH(TRUE(), [Customers] >= 5, 1)",
         "CASE WHEN COALESCE(COUNT(Sales.Customer), 0) >= 5 THEN 1 END"),
        ("[Total Sales] > 0 && [Customers] > 0",
         "COALESCE(SUM(Sales.Amount), 0) > 0 AND COALESCE(COUNT(Sales.Customer), 0) > 0"),
        ("OR([Total Sales] > 0, NOT([Customers] > 0))",
         "COALESCE(SUM(Sales.Amount), 0) > 0 OR NOT COALESCE(COUNT(Sales.Customer), 0) > 0"),
        ("[Total Sales] == BLANK()", "SUM(Sales.Amount) IS NOT DISTINCT FROM NULL"),
        ("IF(TRUE, 1, 0)", "CASE WHEN TRUE THEN 1 ELSE 0 END"),
        ("NOT [Total Sales] > 0", "NOT COALESCE(SUM(Sales.Amount), 0) > 0"),
    ],
)
def test_translates_logic(dax, expected):
    sql, reason = translate_measure(dax, resolve, LOGIC)
    assert reason is None, reason
    assert sql == expected


@pytest.mark.parametrize(
    "dax, fragment",
    [
        ('SWITCH([Customers], 1, "one", "many")', "SWITCH matching a value"),
        ('IF([Total Sales] > 0, "High", 0)', "results of different types"),
        ('"a" = "b"', "comparison on a text value"),
        ("ISBLANK([Total Sales]) + 1", "arithmetic or comparison on a boolean"),
        ("IF(ISBLANK([Total Sales]), TRUE(), BLANK()) && TRUE()", "logical operand"),
        ("IF('Sales'[Amount] > 0, 1, 0)", "bare column reference"),
        ("COALESCE([Total Sales])", "at least two arguments"),
        ('IF("yes", 1, 0)', "is not a condition"),
        ("NOT(TRUE(), FALSE())", "NOT takes exactly one argument"),
        ("ISBLANK()", "ISBLANK takes exactly one argument"),
        ("ISBLANK(TRUE())", "ISBLANK of a boolean"),
        ("SWITCH(TRUE, 1)", "needs at least one condition and result"),
        ("{1, 2}", "later tier"),
    ],
)
def test_logic_refusals_carry_a_reason(dax, fragment):
    sql, reason = translate_measure(dax, resolve, LOGIC)
    assert sql is None
    assert fragment in reason


@pytest.mark.parametrize(
    "dax, rows, dax_result",
    [
        # BLANK > 0 is FALSE in DAX (BLANK compares as 0), so the else branch.
        ("IF(SUM('Sales'[Amount]) > 0, 1, 0)", [], 0),
        # BLANK = 0 is TRUE in DAX.
        ("IF(SUM('Sales'[Amount]) = 0, 1, 0)", [], 1),
        # BLANK == 0 is FALSE (strict), BLANK == BLANK is TRUE.
        ("IF(SUM('Sales'[Amount]) == 0, 1, 0)", [], 0),
        ("IF(SUM('Sales'[Amount]) == BLANK(), 1, 0)", [], 1),
        # IF without a value if false returns BLANK.
        ("IF(SUM('Sales'[Amount]) > 0, 1)", [], None),
        # A BLANK numeric test is FALSE.
        ("IF(SUM('Sales'[Amount]), 1, 0)", [], 0),
        ("IF(SUM('Sales'[Amount]), 1, 0)", [(5.0, "a")], 1),
        ("COALESCE(SUM('Sales'[Amount]), 7)", [], 7),
        ("SWITCH(TRUE, SUM('Sales'[Amount]) > 10, 2, SUM('Sales'[Amount]) > 1, 1, 0)",
         [(5.0, "a")], 1),
    ],
)
def test_logic_sql_matches_dax_blank_semantics(dax, rows, dax_result):
    sql, reason = translate_measure(dax, resolve, LOGIC)
    assert reason is None, reason
    assert _evaluate(sql, rows) == dax_result


@pytest.mark.parametrize("dax", [
    'SWITCH(TRUE, [Total Sales] > 100, "A", "C")',
    "[Total Sales] > 0 && NOT([Customers] = 0)",
    "[Total Sales] == BLANK()",
])
def test_logic_sql_parses(dax):
    sqlglot = pytest.importorskip("sqlglot")
    sql, reason = translate_measure(dax, resolve, LOGIC)
    assert reason is None, reason
    sqlglot.parse_one(sql)


# --- Math (PR3b): translated only for never-BLANK arguments ------------------


@pytest.mark.parametrize(
    "dax, expected",
    [
        ("ABS(COALESCE([Total Sales], 0))", "ABS(COALESCE(SUM(Sales.Amount), 0))"),
        ("SIGN(-3)", "SIGN(-3)"),
        ("SQRT(16)", "SQRT(16)"),
        ("POWER(2, 3) + 1", "POWER(2, 3) + 1"),
        ("EXP(0)", "EXP(0)"),
        ("LN(1)", "LN(1)"),
        # DAX LOG(number, base) defaults to base 10; the spec writes LOG(base, x).
        ("LOG(100)", "LOG10(100)"),
        ("LOG(8, 2)", "LOG(2, 8)"),
        ("LOG10(1000)", "LOG10(1000)"),
        ("INT(-8.9)", "FLOOR(-8.9)"),
        ("TRUNC(-8.9)", "TRUNC(-8.9, 0)"),
        ("ROUNDDOWN(3.14159, 2)", "TRUNC(3.14159, 2)"),
        ("QUOTIENT(-10, 3)", "TRUNC(CAST(-10 AS DOUBLE) / 3, 0)"),
        ("MOD(-3, 2)", "-3 - 2 * FLOOR(CAST(-3 AS DOUBLE) / 2)"),
        ("PI()", "PI()"),
        ("DEGREES(PI())", "DEGREES(PI())"),
        ("VAR s = COALESCE([Total Sales], 0) RETURN SQRT(s)",
         "SQRT(COALESCE(SUM(Sales.Amount), 0))"),
    ],
)
def test_translates_math(dax, expected):
    sql, reason = translate_measure(dax, resolve, LOGIC)
    assert reason is None, reason
    assert sql == expected


@pytest.mark.parametrize(
    "dax, fragment",
    [
        ("ABS([Total Sales])", "may be BLANK"),
        ("ROUND(2.5, 0)", "ties half away from zero"),
        ("ROUNDUP(2.1, 0)", "ROUNDUP"),
        ("CEILING(2.1, 1)", "significance"),
        ("FLOOR(2.1, 1)", "significance"),
        ("POWER(2)", "POWER takes 2 argument(s)"),
        ("ABS(TRUE())", "boolean"),
        ("MROUND(7, 2)", "not translated yet"),
    ],
)
def test_math_refusals_carry_a_reason(dax, fragment):
    sql, reason = translate_measure(dax, resolve, LOGIC)
    assert sql is None
    assert fragment in reason


def _evaluate_math(sql):
    """Run a constant math expression in SQLite. SQLite has no two-argument TRUNC, so the
    spec's TRUNC(x, d) (truncate toward zero to d decimals) is registered for the test."""
    import math
    import sqlite3

    def trunc(x, d):
        factor = 10 ** d
        return math.trunc(x * factor) / factor

    connection = sqlite3.connect(":memory:")
    try:
        connection.create_function("TRUNC", 2, trunc)
        return connection.execute(f"SELECT {sql}").fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "dax, dax_result",
    [
        # MOD takes the sign of the divisor (Microsoft's examples: MOD(3,2)=1, MOD(-3,-2)=-1).
        ("MOD(3, 2)", 1),
        ("MOD(-3, -2)", -1),
        ("MOD(-3, 2)", 1),
        ("MOD(3, -2)", -1),
        ("QUOTIENT(-10, 3)", -3),
        ("QUOTIENT(10, 3)", 3),
        ("INT(-8.9)", -9),
        ("TRUNC(-8.9)", -8),
        ("ROUNDDOWN(3.14159, 2)", 3.14),
        ("LOG(100)", 2),
        ("LOG(8, 2)", 3),
        ("POWER(2, 3)", 8),
        ("ABS(-2.5)", 2.5),
    ],
)
def test_math_sql_matches_dax_results(dax, dax_result):
    sql, reason = translate_measure(dax, resolve, LOGIC)
    assert reason is None, reason
    assert _evaluate_math(sql) == pytest.approx(dax_result)

# --- Robustness: size and depth limits (review on #27) -----------------------


def _chain(levels):
    """M1 = [A] - [A], M2 = [M1] - [A], ...: each level repeats both nullable operands."""
    measures = {"A": "SUM(Sales[Amount])", "M1": "[A] - [A]"}
    for level in range(2, levels + 1):
        measures[f"M{level}"] = f"[M{level - 1}] - [A]"
    return measures


def test_a_short_measure_chain_still_translates():
    sql, reason = translate_measure("[M3]", resolve, _chain(3))
    assert reason is None, reason
    assert len(sql) < 4000


def test_a_long_chain_of_guarded_measures_is_refused_not_emitted():
    # Each level at least doubles the SQL; without a cap 30 levels are gigabytes.
    sql, reason = translate_measure("[M30]", resolve, _chain(30))
    assert sql is None
    assert "too large to translate exactly" in reason


def test_a_deeply_nested_expression_is_refused_not_raised():
    sql, reason = translate_measure("(" * 2000 + "1" + ")" * 2000, resolve)
    assert sql is None
    assert reason == "the expression is too deeply nested to translate"


def test_a_very_long_chain_of_measure_references_is_refused_not_raised():
    measures = {"B0": "SUM(Sales[Amount])"}
    for level in range(1, 3000):
        measures[f"B{level}"] = f"[B{level - 1}]"
    sql, reason = translate_measure("[B2999]", resolve, measures)
    assert sql is None
    assert "too deeply nested" in reason


def test_a_too_deep_measure_does_not_stop_the_import(bim):
    model = _bim_with_measures(bim, {"Deep": "(" * 2000 + "1" + ")" * 2000})
    document = _build(model, translate_dax=True)
    assert list(_dialects(_metric(document, "Deep"))) == ["DAX"]
    assert _dialects(_metric(document, "Total Sales"))["OSSIE_SQL_2026"] == "SUM(Sales.Amount)"


def test_a_refusal_deep_in_a_chain_names_only_the_measure_where_it_failed():
    measures = {"C0": "COUNTROWS(Sales)"}
    for level in range(1, 50):
        measures[f"C{level}"] = f"[C{level - 1}] * 2"
    sql, reason = translate_measure("[C49]", resolve, measures)
    assert sql is None
    assert reason.startswith("referenced measure [C0] is not translated:")
    assert reason.count("referenced measure") == 1


def test_the_size_cap_also_covers_mod_repeating_its_operands():
    # MOD writes both operands twice (n - d * FLOOR(n / d)), so nesting doubles the SQL.
    expr = "COALESCE(SUM(Sales[Amount]), 0)"
    for _ in range(25):
        expr = f"MOD({expr}, 7)"
    sql, reason = translate_measure(expr, resolve)
    assert sql is None
    assert "too large to translate exactly" in reason
