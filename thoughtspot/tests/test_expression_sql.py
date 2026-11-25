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

"""The two places a portable expression lives.

apache/ossie#617 moved the portable expression to a top-level `expression.sql`
field; `expression.dialects[]` keeps carrying it too, tagged. apache/ossie-
converters#79 tracks the migration across every converter and is explicitly
readers-first:

    1. Read both formats. Update converters to recognize portable expressions
    in either location, preferring expression.sql when both are present.
    2. Write the new format... Stage 1 should be completed before stage 2 to
    ensure converters can continue reading documents produced by both older
    and newer versions.

This file is stage 1 and only stage 1. The reader takes both locations from
now; the writer still emits `dialects[]` and nothing here lets it move --
`TestTheWriterHasNotMoved` is what holds that line.

Precedence follows the merged databricks reader (#189,
`OssieConverterCommon.java`), which orders its own vendor dialect, then
`expression.sql`, then `ANSI_SQL`, then `OSSIE_SQL_2026`. Honeydew (#182)
moved to the same `expression`-mapping shape but is NOT precedent for the
vendor-native step: it has no vendor dialect entry of its own and returns
`sql` first unconditionally.

Here the vendor-native entry is THOUGHTSPOT, and it outranks `sql` for
round-trip fidelity rather than preference -- it is the verbatim text a prior
TML -> Ossie trip preserved. Everything else a document carries is
superseded by a usable `sql`, foreign vendor dialects included: two of the
three readers scan for a bare column identifier in list order across any
dialect, so a surviving loser is read, not ignored.
"""
import pytest

from ossie_thoughtspot.constants import DIALECT, PORTABLE_DIALECTS_ACCEPTED
from ossie_thoughtspot.issues import IssueLog
from ossie_thoughtspot.ossie_to_thoughtspot import (
    build_table,
    to_thoughtspot_expression,
)
from ossie_thoughtspot.ossie_to_thoughtspot import convert as ossie_to_tml_convert
from ossie_thoughtspot.tml import DocumentSet, TmlDocument
from ossie_thoughtspot.tml_to_ossie import convert as tml_to_ossie_convert

INDEX = {"orders.amount": ("orders", "Amount")}


def _resolve(index=None):
    return (index if index is not None else INDEX).get


def _dialects(*pairs):
    return [{"dialect": d, "expression": e} for d, e in pairs]


def _field(name, expression, *, datatype=None):
    field = {"name": name, "expression": expression}
    if datatype is not None:
        field["datatype"] = datatype
    return field


def _dataset(fields, name="orders", source="SALES.PUBLIC.ORDERS"):
    return {"name": name, "source": source, "fields": fields}


def _document(fields):
    return {"version": "0.2.0.dev0", "name": "m", "datasets": [_dataset(fields)]}


class TestTheReaderTakesEitherLocation:
    def test_a_sql_only_expression_is_translated(self):
        log = IssueLog()
        result = to_thoughtspot_expression(
            {"sql": "SUM(orders.amount)"}, _resolve(), log, object_ref="metric:m",
        )
        assert result == "sum ( [orders::Amount] )"
        assert not log.as_dicts()

    def test_a_sql_only_qualified_reference_to_another_field_is_translated(self):
        # Deliberately a reference to a DIFFERENT field. A `sql` that names
        # the field it is defined on (`amount` resolving to `orders.amount`)
        # is a self-reference: it never becomes a physical column, so end to
        # end it is reported and dropped, not translated. Asserting the
        # self-reference shape here would give confidence for something the
        # converter rejects.
        log = IssueLog()
        result = to_thoughtspot_expression(
            {"sql": "orders.amount"}, _resolve(), log, object_ref="field:net",
        )
        assert result == "[orders::Amount]"
        assert not log.as_dicts()

    def test_the_dialects_location_still_reads(self):
        # The half that already worked must keep working; this is the control
        # that proves a stage-1 reader did not become a sql-only reader.
        log = IssueLog()
        result = to_thoughtspot_expression(
            {"dialects": _dialects(("ANSI_SQL", "SUM(orders.amount)"))},
            _resolve(), log, object_ref="metric:m",
        )
        assert result == "sum ( [orders::Amount] )"
        assert not log.as_dicts()

    def test_an_expression_with_neither_location_still_raises(self):
        log = IssueLog()
        result = to_thoughtspot_expression({}, _resolve(), log, object_ref="metric:m")
        assert result is None
        assert [i["code"] for i in log.as_dicts()] == ["TS-EXPR-NO-USABLE-DIALECT"]


class TestPrimarySqlOutranksATaggedSibling:
    """apache/ossie#79: "preferring expression.sql when both are present"."""

    @pytest.mark.parametrize("tagged", PORTABLE_DIALECTS_ACCEPTED)
    def test_sql_wins_over_a_tagged_portable_that_disagrees(self, tagged):
        # The #183 interop fixture shape: a tagged portable entry that says
        # something DIFFERENT. Reading the loser silently produces a model
        # built on the wrong column, so the two must not be interchangeable.
        log = IssueLog()
        result = to_thoughtspot_expression(
            {"sql": "orders.amount", "dialects": _dialects((tagged, "orders.wrong"))},
            _resolve(), log, object_ref="field:f",
        )
        assert result == "[orders::Amount]"

    def test_a_thoughtspot_entry_still_wins_over_sql(self):
        # Round-trip fidelity: this is the verbatim text we ourselves wrote.
        log = IssueLog()
        result = to_thoughtspot_expression(
            {"sql": "orders.amount",
             "dialects": _dialects((DIALECT, "sum (   [orders::Amount]  )"))},
            _resolve(), log, object_ref="metric:m",
        )
        assert result == "sum (   [orders::Amount]  )"
        assert not log.as_dicts()


class TestAnUnusableSqlFallsBackRatherThanWinning:
    """`sql` only supersedes a tagged sibling when it carries an expression."""

    @pytest.mark.parametrize("unusable", ["", "   ", None, 7, [], {}])
    def test_an_unusable_sql_leaves_the_tagged_entry_in_charge(self, unusable):
        log = IssueLog()
        result = to_thoughtspot_expression(
            {"sql": unusable,
             "dialects": _dialects(("ANSI_SQL", "SUM(orders.amount)"))},
            _resolve(), log, object_ref="metric:m",
        )
        assert result == "sum ( [orders::Amount] )"
        assert not log.as_dicts()

    def test_a_non_mapping_expression_is_not_a_crash(self):
        log = IssueLog()
        assert to_thoughtspot_expression("SUM(x)", _resolve(), log, object_ref="m:m") is None
        assert [i["code"] for i in log.as_dicts()] == ["TS-EXPR-NO-USABLE-DIALECT"]


class TestASqlOnlyFieldBecomesATableColumn:
    """`build_table`'s physical/computed split reads the same two locations."""

    def test_a_sql_only_bare_identifier_is_a_physical_column(self):
        log = IssueLog()
        table = build_table(_dataset([_field("amount", {"sql": "amount"})]), log)
        columns = {c["name"]: c for c in table.body["columns"]}
        assert "amount" in columns
        assert columns["amount"]["db_column_name"] == "amount"

    def test_a_sql_only_field_raises_no_missing_expression_warning(self):
        log = IssueLog()
        build_table(_dataset([_field("amount", {"sql": "amount"})]), log)
        assert "TS-FIELD-NO-EXPRESSION" not in [i["code"] for i in log.as_dicts()]

    def test_a_field_with_no_expression_at_all_still_warns(self):
        log = IssueLog()
        build_table(_dataset([{"name": "amount"}]), log)
        assert "TS-FIELD-NO-EXPRESSION" in [i["code"] for i in log.as_dicts()]

    @pytest.mark.parametrize("tagged", PORTABLE_DIALECTS_ACCEPTED)
    def test_the_column_comes_from_sql_when_a_tagged_sibling_names_another(self, tagged):
        # The structural reader picks a bare identifier out of ANY entry, so
        # unless the superseded sibling is removed from the list it can win
        # here even while `to_thoughtspot_expression` correctly prefers
        # `sql` -- and the table would then read a different warehouse column
        # than the model's expression does, silently.
        log = IssueLog()
        table = build_table(_dataset([_field(
            "amount",
            {"sql": "amount", "dialects": _dialects((tagged, "superseded_column"))},
        )]), log)
        assert [c["db_column_name"] for c in table.body["columns"]] == ["amount"]


class TestASqlEntrySupersedesEveryOtherDialect:
    """Not only the portable ones. The bare-identifier scan reads any dialect."""

    @pytest.mark.parametrize("vendor", ["DATABRICKS", "SNOWFLAKE"])
    def test_a_foreign_vendor_entry_does_not_name_the_warehouse_column(self, vendor):
        # Before this was fixed the Table read `legacy_amt` -- a different
        # warehouse column than the authoritative `sql` names -- silently.
        log = IssueLog()
        table = build_table(_dataset([_field(
            "amount",
            {"sql": "amount", "dialects": _dialects((vendor, "legacy_amt"))},
        )]), log)
        assert [c["db_column_name"] for c in table.body["columns"]] == ["amount"]

    def test_a_foreign_vendor_entry_cannot_make_a_computed_field_physical(self):
        # The worse shape: `sql` is an aggregate, so the field is computed.
        # A bare vendor entry beside it produced a physical column, no
        # formula and no issue at all.
        log = IssueLog()
        table = build_table(_dataset([_field(
            "revenue",
            {"sql": "SUM(orders.qty)", "dialects": _dialects(("DATABRICKS", "amt"))},
        )]), log)
        assert table.body["columns"] == []

    def test_the_thoughtspot_entry_is_the_one_that_survives(self):
        log = IssueLog()
        table = build_table(_dataset([_field(
            "amount",
            {"sql": "other", "dialects": _dialects((DIALECT, "[orders::Amount]"))},
        )]), log)
        assert [c["name"] for c in table.body["columns"]] == ["Amount"]

    def test_a_non_mapping_dialects_member_is_still_filtered(self):
        # The isinstance guard moved into the helper when the callers changed;
        # without it this raises AttributeError instead of converting.
        log = IssueLog()
        result = to_thoughtspot_expression(
            {"dialects": ["amount", None, {"dialect": "ANSI_SQL",
                                           "expression": "SUM(orders.amount)"}]},
            _resolve(), log, object_ref="metric:m",
        )
        assert result == "sum ( [orders::Amount] )"


class TestASupersededSiblingIsNotAFallback:
    """A declared loss, not a quiet conversion through the losing entry."""

    def test_an_untranslatable_sql_drops_the_field_rather_than_using_the_sibling(self):
        # On the `dialects`-only reader this document converted: the tagged
        # `REVENUE` is a bare identifier, so the field became a physical
        # column. Now `sql` is authoritative and says something this
        # converter cannot translate, so the field is reported and dropped.
        # Converting through `REVENUE` would build a model on an expression
        # the document itself marks as not authoritative.
        result = ossie_to_tml_convert(_document([
            _field("qty", {"dialects": _dialects(("ANSI_SQL", "QTY"))}),
            _field("revenue", {"sql": "orders.qty * orders.price",
                               "dialects": _dialects(("ANSI_SQL", "REVENUE"))}),
        ]))
        codes = [i["code"] for i in result.issues.as_dicts()]
        assert "TS-MODEL-FIELD-UNTRANSLATABLE" in codes

        table_columns = {c["name"] for c in result.documents.tables[0].body["columns"]}
        assert "revenue" not in table_columns
        assert "qty" in table_columns


class TestMetricsReadTheSqlLocationToo:
    """`_build_metric` has its own call site; fields passing proves nothing."""

    def test_a_model_scoped_metric_reads_sql(self):
        document = _document([_field("amount", {"dialects": _dialects(("ANSI_SQL", "amount"))})])
        document["metrics"] = [{"name": "total", "expression": {"sql": "SUM(orders.amount)"}}]
        result = ossie_to_tml_convert(document)

        formulas = {f["name"]: f["expr"] for f in result.documents.model.body["formulas"]}
        assert formulas["total"] == "sum ( [orders::amount] )"
        assert "TS-MODEL-METRIC-UNTRANSLATABLE" not in [
            i["code"] for i in result.issues.as_dicts()
        ]

    def test_a_dataset_scoped_metric_reads_sql_too(self):
        """The positive form of a gap this file used to pin.

        Every metric this converter read once came from the document root, so
        a metric under `datasets[].metrics` was dropped before its expression
        was looked at and reading `sql` could not help it. The dataset-scoped
        path exists now (apache/ossie-converters#101), and it picks the
        expression location the same way the model-scoped path does.

        The name is single-part because that is the only spelling Metric
        Scoping rule 1 allows here. `SUM(orders.amount)`, which this test used
        while the path was missing, is the model-scoped spelling and is
        refused below.
        """
        document = _document([_field("amount", {"dialects": _dialects(("ANSI_SQL", "amount"))})])
        document["datasets"][0]["metrics"] = [
            {"name": "total", "expression": {"sql": "SUM(amount)"}}
        ]
        result = ossie_to_tml_convert(document)

        formulas = {f["name"]: f["expr"] for f in result.documents.model.body["formulas"]}
        assert formulas["total"] == "sum ( [orders::amount] )"
        # The same convention note the model-scoped path raises, which is the
        # point: the two paths differ in namespace, not in treatment.
        assert [i["code"] for i in result.issues.as_dicts()] == [
            "TS-DATASET-CONNECTION-MISSING",
            "TS-MODEL-METRIC-AGGREGATION-CONVENTION",
        ]

    def test_a_dataset_scoped_metric_does_not_read_the_qualified_spelling(self):
        # Rule 1 again, from the other side: letting `dataset.field` resolve
        # inside a dataset-scoped metric would collapse the two namespaces,
        # and a metric that reaches another dataset would silently become
        # legal here instead of being moved to the model root.
        document = _document([_field("amount", {"dialects": _dialects(("ANSI_SQL", "amount"))})])
        document["datasets"][0]["metrics"] = [
            {"name": "total", "expression": {"sql": "SUM(orders.amount)"}}
        ]
        result = ossie_to_tml_convert(document)

        assert "formulas" not in result.documents.model.body
        assert [i["code"] for i in result.issues.as_dicts()] == [
            "TS-DATASET-CONNECTION-MISSING",
            "TS-EXPR-ANSI-UNRESOLVED",
            "TS-MODEL-METRIC-UNTRANSLATABLE",
        ]

    def test_a_computed_field_reads_sql(self):
        # The end-to-end case below uses physical fields only, which never
        # reach `_build_field`'s computed branch.
        document = _document([
            _field("amount", {"dialects": _dialects(("ANSI_SQL", "amount"))}),
            _field("doubled", {"sql": "SUM(orders.amount)"}),
        ])
        result = ossie_to_tml_convert(document)
        formulas = {f["name"]: f["expr"] for f in result.documents.model.body["formulas"]}
        assert formulas["doubled"] == "sum ( [orders::amount] )"


def test_a_sql_only_field_survives_end_to_end():
    """The measured failure this change exists to fix.

    Before stage 1 the `sql`-only field was dropped from the model with
    TS-EXPR-NO-USABLE-DIALECT + TS-MODEL-FIELD-UNTRANSLATABLE, both ERROR,
    while the `dialects` control beside it converted cleanly. Asserted end to
    end rather than at the expression boundary, because that is where the
    loss was visible.
    """
    result = ossie_to_tml_convert(_document([
        _field("amount", {"sql": "amount"}, datatype="Decimal"),
        _field("region", {"dialects": _dialects(("ANSI_SQL", "region"))}, datatype="String"),
    ]))

    assert [i["code"] for i in result.issues.as_dicts() if i["severity"] == "ERROR"] == []

    model_columns = {c["name"] for c in result.documents.model.body["columns"]}
    assert {"amount", "region"} <= model_columns

    table_columns = {c["name"] for c in result.documents.tables[0].body["columns"]}
    assert {"amount", "region"} <= table_columns


class TestTheWriterNowEmitsBoth:
    """Stage 2. The reader above came first and is unchanged by this.

    Until this branch the writer emitted `dialects` only, and the test here
    pinned that deliberately -- apache/ossie-converters#79 stages the
    migration readers-first, so a converter must be able to READ both
    locations before any converter starts WRITING the new one. #203 is that
    reader for ThoughtSpot. With it in place the writer can move.

    `test_expression_sql_emitted.py` carries the rest of this behaviour; what
    is pinned here is the pairing -- `sql` arrives beside `dialects`, never
    instead of it.
    """

    def test_the_writer_emits_sql_beside_dialects(self):
        table_doc = TmlDocument(
            kind="table",
            body={
                "name": "ORDERS", "db": "SALES", "schema": "PUBLIC", "db_table": "ORDERS",
                "connection": {"name": "My Snowflake"},
                "columns": [
                    {"name": "Amount", "db_column_name": "O_AMOUNT",
                     "db_column_properties": {"data_type": "DOUBLE"}},
                ],
            },
            guid=None,
        )
        model_doc = TmlDocument(
            kind="model",
            body={
                "name": "Sales Analytics",
                "model_tables": [{"name": "ORDERS"}],
                "columns": [
                    {"name": "Amount", "column_id": "ORDERS::Amount",
                     "properties": {"column_type": "ATTRIBUTE"}},
                ],
            },
            guid=None,
        )
        ossie = tml_to_ossie_convert(DocumentSet(model=model_doc, tables=(table_doc,)))

        fields = [f for d in ossie.model["datasets"] for f in (d.get("fields") or [])]
        assert fields
        for field in fields:
            assert "dialects" in field["expression"]
            assert field["expression"]["sql"] == "O_AMOUNT"
