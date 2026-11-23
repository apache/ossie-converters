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

"""One aggregate over one declared column is portable, and was not emitted.

apache/ossie-converters#30: every ThoughtSpot metric came out carrying a
THOUGHTSPOT dialect and nothing else, so no other vendor could evaluate one.

The cause was narrower than the issue suggests. A metric built from a COLUMN
plus an `aggregation` property already composed a portable sibling
(`_compose_aggregate_entries`). A metric built from a FORMULA did not --
`expression_entries` emitted a portable sibling only for a bare column
reference, and a formula is a call, so every formula-backed metric fell
through to `TS-EXPR-THOUGHTSPOT-ONLY`.

That swept up the easy ones with the hard ones. `sum ( [store_sales::price] )`
is a single aggregate over a column the document already declares as a field.
It needs no expression parser, no inlining and no new catalog: the aggregate
name maps to a catalog row, the operand resolves through the same resolver a
bare reference uses, and the composition is the one the column-plus-
aggregation path already performs.

What stays unportable is unchanged, and deliberately so: a compound
expression, a formula cross-reference, a nested call, a runtime parameter, or
an operand naming a column the Model does not surface.
"""
from ossie_thoughtspot.constants import PORTABLE_DIALECT
from ossie_thoughtspot.tml import DocumentSet, TmlDocument
from ossie_thoughtspot.tml_to_ossie import convert


def _table(name, columns):
    return TmlDocument(kind="table", body={
        "name": name, "db": "SALES", "schema": "PUBLIC", "db_table": name,
        "connection": {"name": "My Snowflake"},
        "columns": [{"name": c, "db_column_name": c,
                     "db_column_properties": {"data_type": "INT64"}} for c in columns],
    }, guid=None)


#: The surfacing attribute columns. A metric's operand resolves only to a
#: field the Model declares, so these are what make the aggregate portable.
SURFACED = [
    {"name": "Amount", "column_id": "ORDERS::AMOUNT",
     "properties": {"column_type": "ATTRIBUTE"}},
    {"name": "Cust", "column_id": "ORDERS::CUST_ID",
     "properties": {"column_type": "ATTRIBUTE"}},
]


def _convert(expr, *, surfaced=True, name="Total"):
    columns = (list(SURFACED) if surfaced else []) + [
        {"name": name, "formula_id": "f1", "properties": {"column_type": "MEASURE"}},
    ]
    model = TmlDocument(kind="model", body={
        "name": "sales",
        "model_tables": [{"name": "ORDERS"}],
        "columns": columns,
        "formulas": [{"id": "f1", "name": name, "expr": expr}],
    }, guid=None)
    return convert(DocumentSet(
        model=model, tables=(_table("ORDERS", ["AMOUNT", "CUST_ID"]),)))


def _ansi(result, metric_name):
    metric = next(m for m in result.model["metrics"] if m["name"] == metric_name)
    return next((e["expression"] for e in metric["expression"]["dialects"]
                 if e["dialect"] == PORTABLE_DIALECT), None)


def _codes(result):
    return [i.get("code") for i in result.issues.as_dicts()]


def _convert_attribute(expr, *, name="Test"):
    """The same formula on an ATTRIBUTE column, so it becomes a FIELD not a metric."""
    columns = list(SURFACED) + [
        {"name": name, "formula_id": "f1", "properties": {"column_type": "ATTRIBUTE"}},
    ]
    model = TmlDocument(kind="model", body={
        "name": "sales",
        "model_tables": [{"name": "ORDERS"}],
        "columns": columns,
        "formulas": [{"id": "f1", "name": name, "expr": expr}],
    }, guid=None)
    return convert(DocumentSet(
        model=model, tables=(_table("ORDERS", ["AMOUNT", "CUST_ID"]),)))


def _field_ansi(result, field_name):
    dataset = next(d for d in result.model["datasets"] if d["name"] == "ORDERS")
    field = next(f for f in dataset["fields"] if f["name"] == field_name)
    return next((e["expression"] for e in field["expression"]["dialects"]
                 if e["dialect"] == PORTABLE_DIALECT), None)


class TestAFieldNeverCarriesAnAggregate:
    """An aggregate is a metric's shape. A field is a row-level attribute
    (core-spec/spec.md), so `sum(...)` has no honest field expression: the
    qualified form resolves to nothing, and the bare form claims a row-level
    value the column does not have. ThoughtSpot itself runs such a column as a
    measure. Raised by @christianeu-db reviewing apache/ossie-converters#209.
    """

    def test_an_aggregating_attribute_gets_no_portable_sibling(self):
        result = _convert_attribute("sum ( [ORDERS::AMOUNT] )")
        assert _field_ansi(result, "test") is None
        assert "TS-EXPR-THOUGHTSPOT-ONLY" in _codes(result)

    def test_a_bare_attribute_still_gets_its_unqualified_sibling(self):
        result = _convert_attribute("[ORDERS::AMOUNT]")
        assert _field_ansi(result, "test") == "AMOUNT"

    def test_the_metric_path_is_unchanged(self):
        assert _ansi(_convert("sum ( [ORDERS::AMOUNT] )"), "total") == "SUM(ORDERS.AMOUNT)"


class TestASingleAggregateOverADeclaredColumnBecomesPortable:

    def test_sum_composes(self):
        assert _ansi(_convert("sum ( [ORDERS::AMOUNT] )"), "total") == "SUM(ORDERS.AMOUNT)"

    def test_the_verbatim_thoughtspot_entry_is_untouched(self):
        result = _convert("sum ( [ORDERS::AMOUNT] )")
        metric = next(m for m in result.model["metrics"] if m["name"] == "total")
        [verbatim] = [e for e in metric["expression"]["dialects"]
                      if e["dialect"] == "THOUGHTSPOT"]
        assert verbatim["expression"] == "sum ( [ORDERS::AMOUNT] )"

    def test_the_non_portability_issue_is_no_longer_raised(self):
        assert "TS-EXPR-THOUGHTSPOT-ONLY" not in _codes(_convert("sum ( [ORDERS::AMOUNT] )"))

    def test_every_catalog_aggregate_follows(self):
        for call, expected in (
            ("sum", "SUM(ORDERS.AMOUNT)"),
            ("average", "AVG(ORDERS.AMOUNT)"),
            ("min", "MIN(ORDERS.AMOUNT)"),
            ("max", "MAX(ORDERS.AMOUNT)"),
            ("count", "COUNT(ORDERS.AMOUNT)"),
            ("stddev", "STDDEV(ORDERS.AMOUNT)"),
            ("variance", "VARIANCE(ORDERS.AMOUNT)"),
        ):
            assert _ansi(_convert(f"{call} ( [ORDERS::AMOUNT] )"), "total") == expected, call

    def test_unique_count_becomes_count_distinct(self):
        # Two words in ThoughtSpot, one construct, and the ANSI form needs
        # DISTINCT inside the parentheses rather than a different function.
        assert _ansi(_convert("unique count ( [ORDERS::CUST_ID] )"), "total") == (
            "COUNT(DISTINCT ORDERS.CUST_ID)"
        )

    def test_the_call_name_is_matched_case_insensitively(self):
        assert _ansi(_convert("SUM ( [ORDERS::AMOUNT] )"), "total") == "SUM(ORDERS.AMOUNT)"


class TestWhatStaysUnportableStaysUnportable:
    """Each of these needs machinery this change does not add."""

    def _thoughtspot_only(self, expr):
        result = _convert(expr)
        return _ansi(result, "total") is None and "TS-EXPR-THOUGHTSPOT-ONLY" in _codes(result)

    def test_a_compound_expression(self):
        assert self._thoughtspot_only(
            "sum ( [ORDERS::AMOUNT] ) / unique count ( [ORDERS::CUST_ID] )"
        )

    def test_a_nested_call(self):
        assert self._thoughtspot_only("sum ( round ( [ORDERS::AMOUNT] , 2 ) )")

    def test_an_aggregate_over_an_unsurfaced_column(self):
        result = _convert("sum ( [ORDERS::AMOUNT] )", surfaced=False)
        assert _ansi(result, "total") is None

    def test_a_non_aggregate_call(self):
        # `round` is not an aggregate, so there is no aggregation to compose.
        assert self._thoughtspot_only("round ( [ORDERS::AMOUNT] , 2 )")

    def test_a_conditional_aggregate(self):
        # `sum_if` is a genuine ThoughtSpot aggregate with no TML `aggregation`
        # enum counterpart, so it maps to no catalog row at all.
        assert self._thoughtspot_only("sum_if ( [ORDERS::AMOUNT] > 1 , [ORDERS::AMOUNT] )")

    def test_a_catalog_aggregate_carrying_a_second_argument(self):
        # Not valid ThoughtSpot, but the arity guard is what stands between a
        # malformed document and a composed expression that silently drops an
        # operand. Without it this renders `SUM(ORDERS.AMOUNT)` and loses the
        # rest.
        assert self._thoughtspot_only(
            "sum ( [ORDERS::AMOUNT] , [ORDERS::CUST_ID] )"
        )

    def test_a_single_argument_call_that_is_not_an_aggregate(self):
        # One argument, resolvable operand, and still not portable: the name
        # has to name an aggregate. `round` with one argument passes every
        # other test this composer applies.
        assert self._thoughtspot_only("round ( [ORDERS::AMOUNT] )")
