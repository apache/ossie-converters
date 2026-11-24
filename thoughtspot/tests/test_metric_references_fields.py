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

"""A model-scoped metric's portable expression names fields, not columns.

core-spec "Metric Scoping", rule 5: a model-scoped metric references fields
as `dataset.field` and cannot reference source columns. The writer composed
the reference from the warehouse column instead -- `SUM(ORDERS.REV_AMT)` for a
column the model surfaces as the field `revenue` -- which nothing could
resolve. The core validator warned that `ORDERS.REV_AMT` is not a declared
field, and this converter's own reader dropped the metric whenever no
THOUGHTSPOT entry was there to fall back on.

Every fixture that pinned the old output had a warehouse column equal to its
field name up to case (`AMOUNT` and `amount`), so nothing noticed. Each test
here uses a column whose name differs from its field's: `REV_AMT` under the
display name `Revenue`.

A field's own expression is unaffected. It refers to a column of its
dataset's source, so it still names `REV_AMT`.
"""
import copy

from ossie_thoughtspot.constants import PORTABLE_DIALECT
from ossie_thoughtspot.ossie_to_thoughtspot import convert as to_tml
from ossie_thoughtspot.tml import DocumentSet, TmlDocument
from ossie_thoughtspot.tml_to_ossie import convert


def _orders(*columns):
    """The ORDERS table; each column is `(display name, warehouse column)`."""
    return TmlDocument(kind="table", body={
        "name": "ORDERS", "db": "SALES", "schema": "PUBLIC", "db_table": "ORDERS",
        "connection": {"name": "My Snowflake"},
        "columns": [{"name": name, "db_column_name": db_column,
                     "db_column_properties": {"data_type": "DOUBLE"}}
                    for name, db_column in columns],
    }, guid=None)


def _attribute(name, column_id):
    return {"name": name, "column_id": column_id,
            "properties": {"column_type": "ATTRIBUTE"}}


def _measure(name, column_id, aggregation="SUM"):
    return {"name": name, "column_id": column_id,
            "properties": {"column_type": "MEASURE", "aggregation": aggregation}}


def _convert(columns, *, formulas=None, table=None):
    body = {"name": "sales", "model_tables": [{"name": "ORDERS"}], "columns": columns}
    if formulas is not None:
        body["formulas"] = formulas
    return convert(DocumentSet(
        model=TmlDocument(kind="model", body=body, guid=None),
        tables=(table or _orders(("Revenue", "REV_AMT")),),
    ))


def _metric(result, name):
    [metric] = [m for m in result.model.get("metrics") or [] if m["name"] == name]
    return metric


def _ansi(expression):
    return next((e["expression"] for e in expression["dialects"]
                 if e["dialect"] == PORTABLE_DIALECT), None)


REVENUE = _attribute("Revenue", "ORDERS::Revenue")
TOTAL_REVENUE = _measure("Total Revenue", "ORDERS::Revenue")


class TestAMetricNamesTheField:

    def test_sql_names_the_field_not_the_warehouse_column(self):
        expression = _metric(_convert([REVENUE, TOTAL_REVENUE]), "total_revenue")["expression"]
        assert expression["sql"] == "SUM(ORDERS.revenue)"

    def test_the_ansi_entry_agrees(self):
        expression = _metric(_convert([REVENUE, TOTAL_REVENUE]), "total_revenue")["expression"]
        assert _ansi(expression) == expression["sql"]

    def test_the_field_itself_still_names_the_warehouse_column(self):
        [field] = _convert([REVENUE, TOTAL_REVENUE]).model["datasets"][0]["fields"]
        assert field["expression"]["sql"] == "REV_AMT"

    def test_an_unaggregated_metric_names_the_field_too(self):
        result = _convert([REVENUE, _measure("Raw Revenue", "ORDERS::Revenue", "NONE")])
        assert _metric(result, "raw_revenue")["expression"]["sql"] == "ORDERS.revenue"

    def test_a_scalar_formula_with_an_aggregation_names_the_field(self):
        result = _convert(
            [REVENUE, {"name": "Booked", "formula_id": "f_booked",
                       "properties": {"column_type": "MEASURE", "aggregation": "SUM"}}],
            formulas=[{"id": "f_booked", "name": "Booked", "expr": "[ORDERS::Revenue]"}],
        )
        assert _metric(result, "booked")["expression"]["sql"] == "SUM(ORDERS.revenue)"


class TestTheFieldNameIsTheOneActuallyBuilt:
    """The name comes from the field as built, not from a second derivation."""

    def test_a_metric_listed_before_its_field_still_finds_it(self):
        # Fields and metrics used to be converted in one pass in `columns[]`
        # order, so a metric ahead of its field would have found no name.
        result = _convert([TOTAL_REVENUE, REVENUE])
        assert _metric(result, "total_revenue")["expression"]["sql"] == "SUM(ORDERS.revenue)"

    def test_a_renamed_field_is_referenced_under_its_new_name(self):
        # `Revenue` and `revenue` both normalise to `revenue`; the second field
        # is renamed when it collides, and the metric has to follow it.
        result = _convert(
            [REVENUE, _attribute("revenue", "ORDERS::Net"),
             _measure("Total Net", "ORDERS::Net")],
            table=_orders(("Revenue", "REV_AMT"), ("Net", "NET_AMT")),
        )
        fields = {f["label"]: f["name"] for f in result.model["datasets"][0]["fields"]}
        renamed = fields["revenue"]
        assert renamed != "revenue"
        assert _metric(result, "total_net")["expression"]["sql"] == f"SUM(ORDERS.{renamed})"

    def test_a_column_surfaced_by_no_field_still_has_no_portable_form(self):
        # Nothing to name, so nothing is invented.
        expression = _metric(_convert([TOTAL_REVENUE]), "total_revenue")["expression"]
        assert "sql" not in expression
        assert _ansi(expression) is None


class TestTheReferenceResolvesOnTheWayBack:
    """Ossie -> TML resolves `dataset.field`, so the portable form is enough."""

    def test_a_metric_with_only_its_portable_form_converts_back(self):
        document = copy.deepcopy(_convert([REVENUE, TOTAL_REVENUE]).model)
        [metric] = document["metrics"]
        metric["expression"]["dialects"] = [
            e for e in metric["expression"]["dialects"] if e["dialect"] != "THOUGHTSPOT"
        ]
        metric.pop("custom_extensions", None)

        back = to_tml(document)

        codes = [i["code"] for i in back.issues.as_dicts()]
        assert "TS-EXPR-ANSI-UNRESOLVED" not in codes
        assert "TS-MODEL-METRIC-UNTRANSLATABLE" not in codes
        formulas = back.documents.model.body.get("formulas") or []
        assert [f["expr"] for f in formulas] == ["sum ( [ORDERS::Revenue] )"]
