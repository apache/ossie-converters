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

"""The other half of apache/ossie#617: writing `expression.sql`.

`test_expression_sql.py` covers reading it. This covers emitting it, and the
two have to agree -- a document this converter writes is the one it will be
asked to read back.

`sql` is the specification's PRIMARY portable form, so a document that has a
portable expression and does not put it there is conformant but quiet about
it: a consumer reading `sql` alone -- which the spec invites, since either
location may stand alone -- sees nothing. Every portable expression this
converter produced lived only in `dialects[ANSI_SQL]`.

**Additive, never a replacement.** The `ANSI_SQL` entry stays exactly where
it was, because not every reader in this repository reads `sql` yet:
snowflake skips an expression that has only `sql` and no dialect entries. The
two carry the same text: apache/ossie#450 established the dialects parse
identically, so one string is valid as both.

**The THOUGHTSPOT entry still outranks `sql` on the way back in**, which is
what keeps the round trip intact. `portable_expression_entries` returns
`[THOUGHTSPOT, sql]`, the verbatim entry first, so emitting `sql` here
changes nothing about how our own documents are re-read.

Nothing new is translated. An expression with no portable sibling today
still has none, and still carries `TS-EXPR-THOUGHTSPOT-ONLY`.
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


def _model(columns, formulas=None):
    body = {"name": "sales",
            "model_tables": [{"name": "ORDERS"}],
            "columns": columns}
    if formulas is not None:
        body["formulas"] = formulas
    return TmlDocument(kind="model", body=body, guid=None)


def _convert(columns, formulas=None):
    return convert(DocumentSet(
        model=_model(columns, formulas),
        tables=(_table("ORDERS", ["AMOUNT", "QTY", "Custom Clearance Time (min)"]),),
    ))


def _fields(result):
    # Keyed on `label`, the ThoughtSpot display name -- `name` is the
    # normalised Ossie identifier ("amount" for a column shown as "Amount").
    return {f.get("label") or f["name"]: f
            for d in result.model["datasets"] for f in d.get("fields") or []}


def _metrics(result):
    # A metric carries no `label`; its ThoughtSpot display name is stashed.
    return {m["name"]: m for m in result.model.get("metrics") or []}


def _ansi(expression):
    return next((e["expression"] for e in expression.get("dialects") or []
                 if e["dialect"] == PORTABLE_DIALECT), None)


PHYSICAL = [{"name": "Amount", "column_id": "ORDERS::AMOUNT",
             "properties": {"column_type": "ATTRIBUTE"}}]


class TestAPortableFieldExpressionReachesSql:
    """A bare column reference is the shape that already gets an ANSI sibling."""

    def test_sql_carries_the_portable_expression(self):
        field = _fields(_convert(PHYSICAL))["Amount"]
        assert field["expression"]["sql"] == "AMOUNT"

    def test_the_ansi_dialect_entry_is_still_there(self):
        # Removing it would lose the field in snowflake, which skips an
        # expression that has only `sql` and no dialect entries.
        field = _fields(_convert(PHYSICAL))["Amount"]
        assert _ansi(field["expression"]) == "AMOUNT"

    def test_sql_and_the_ansi_entry_agree(self):
        expression = _fields(_convert(PHYSICAL))["Amount"]["expression"]
        assert expression["sql"] == _ansi(expression)

    def test_the_verbatim_thoughtspot_entry_is_untouched(self):
        expression = _fields(_convert(PHYSICAL))["Amount"]["expression"]
        [verbatim] = [e for e in expression["dialects"] if e["dialect"] == "THOUGHTSPOT"]
        assert verbatim["expression"] == "[ORDERS::AMOUNT]"

    def test_a_name_needing_quotes_is_quoted_in_sql_too(self):
        result = _convert([{"name": "Clearance",
                            "column_id": "ORDERS::Custom Clearance Time (min)",
                            "properties": {"column_type": "ATTRIBUTE"}}])
        expression = _fields(result)["Clearance"]["expression"]
        assert expression["sql"] == '"Custom Clearance Time (min)"'
        assert expression["sql"] == _ansi(expression)

    def test_a_formula_field_that_is_a_bare_reference_reaches_sql_too(self):
        # A formula-backed field builds its expression on a separate path from
        # a physical one, so it needs its own test.
        result = _convert(
            PHYSICAL + [{"name": "Amount Copy", "formula_id": "f_copy",
                         "properties": {"column_type": "ATTRIBUTE"}}],
            formulas=[{"id": "f_copy", "name": "Amount Copy",
                       "expr": "[ORDERS::AMOUNT]"}],
        )
        expression = _fields(result)["Amount Copy"]["expression"]
        assert expression["sql"] == "AMOUNT"
        assert expression["sql"] == _ansi(expression)


MEASURE = [{"name": "Total Amount", "column_id": "ORDERS::AMOUNT",
            "properties": {"column_type": "MEASURE", "aggregation": "SUM"}}]


class TestAPortableMetricExpressionReachesSql:
    """A metric's portable sibling keeps its dataset qualifier, in both places."""

    def test_sql_carries_the_aggregate(self):
        metric = _metrics(_convert(PHYSICAL + MEASURE))["total_amount"]
        assert metric["expression"]["sql"] == "SUM(ORDERS.amount)"
        assert metric["expression"]["sql"] == _ansi(metric["expression"])

    def test_a_metric_over_an_unsurfaced_column_still_has_no_portable_form(self):
        # The common shape in a real model, and the reason
        # apache/ossie-converters#30 exists: a MEASURE column the Model does
        # not ALSO surface as an attribute is no Ossie field, so there is
        # nothing for the operand to name and no ANSI sibling is composed.
        # Writing `sql` changes none of that -- it copies a portable form
        # that exists, it does not invent one.
        metric = _metrics(_convert(MEASURE))["total_amount"]
        assert "sql" not in metric["expression"]
        assert _ansi(metric["expression"]) is None


class TestAnUnportableExpressionGainsNothing:
    """`sql` is the portable form; there is still no portable form to put there."""

    def _result(self):
        # The physical column has to be surfaced too, or the formula's
        # reference to it does not resolve and the field is dropped before
        # any expression is composed.
        return _convert(
            PHYSICAL + [{"name": "Doubled", "formula_id": "f1",
                         "properties": {"column_type": "ATTRIBUTE"}}],
            formulas=[{"id": "f1", "name": "Doubled",
                       "expr": "[ORDERS::AMOUNT] * 2"}],
        )

    def test_a_formula_backed_field_has_no_sql(self):
        expression = _fields(self._result())["Doubled"]["expression"]
        assert "sql" not in expression
        assert _ansi(expression) is None

    def test_the_non_portability_is_still_reported(self):
        codes = [i.get("code") for i in self._result().issues.as_dicts()]
        assert "TS-EXPR-THOUGHTSPOT-ONLY" in codes
