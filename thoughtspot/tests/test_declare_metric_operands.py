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

"""A metric's operand is a column this document has to declare too.

The same rule as keys and relationships, from a third source. A metric that
aggregates a warehouse column the Model does not ALSO surface as an
ATTRIBUTE had nothing to name: `resolve` answers only for surfaced columns,
so `_compose_aggregate_entries` composed no portable sibling and the metric
came out THOUGHTSPOT-only.

That is the ordinary shape of a ThoughtSpot model. A measure column is
surfaced once, as a MEASURE; surfacing the same warehouse column a second
time as an ATTRIBUTE is something a modeller does for filtering, not as a
matter of course. In the TPC-DS fixture `total_return_quantity` is exactly
this: `sum(sr_return_quantity)` over a column no field declares.

**Declared BEFORE phase 3, unlike the key and relationship columns.** Those
are declared in phase 5.5 because it is the relationships and keys built in
phases 4 and 5 that decide which columns they need. A metric's operands are
knowable from the Model's own columns, with nothing built yet, and they have
to be: the metric that needs them is composed in phase 3, two phases
earlier, so a field declared afterwards arrives too late to be referenced.

The declared field still produces no TML -- no Model column and no Table
column -- exactly as a key's does, so the round trip is unchanged.
"""
from ossie_thoughtspot.constants import PORTABLE_DIALECT
from ossie_thoughtspot.tml import DocumentSet, TmlDocument
from ossie_thoughtspot.tml_to_ossie import convert


def _table():
    return TmlDocument(kind="table", body={
        "name": "RETURNS", "db": "SALES", "schema": "PUBLIC", "db_table": "RETURNS",
        "connection": {"name": "My Snowflake"},
        "columns": [
            {"name": "Qty", "db_column_name": "RETURN_QTY",
             "db_column_properties": {"data_type": "INT64"}},
            {"name": "Reason", "db_column_name": "REASON_CODE",
             "db_column_properties": {"data_type": "VARCHAR"}},
        ],
    }, guid=None)


def _convert(columns):
    model = TmlDocument(kind="model", body={
        "name": "returns", "model_tables": [{"name": "RETURNS"}], "columns": columns,
    }, guid=None)
    return convert(DocumentSet(model=model, tables=(_table(),)))


#: The ordinary shape: the measure is surfaced, its column is not.
MEASURE_ONLY = [
    {"name": "Reason", "column_id": "RETURNS::Reason",
     "properties": {"column_type": "ATTRIBUTE"}},
    {"name": "Total Qty", "column_id": "RETURNS::Qty",
     "properties": {"column_type": "MEASURE", "aggregation": "SUM"}},
]


def _fields(result):
    return {f.get("label") or f["name"]
            for d in result.model["datasets"] for f in d.get("fields") or []}


def _metric_ansi(result, name):
    metric = next(m for m in result.model["metrics"] if m["name"] == name)
    return next((e["expression"] for e in metric["expression"]["dialects"]
                 if e["dialect"] == PORTABLE_DIALECT), None)


class TestAnOperandTheModelDoesNotSurfaceIsDeclared:

    def test_the_operand_column_becomes_a_field(self):
        assert "Qty" in _fields(_convert(MEASURE_ONLY))

    def test_the_metric_gains_a_portable_expression(self):
        # Qualified, because a metric is model-scoped and may reference any
        # dataset -- the asymmetry `expression_entries` documents, where a
        # field's own `source` already says which table its column sits in.
        assert _metric_ansi(_convert(MEASURE_ONLY), "total_qty") == (
            "SUM(RETURNS.RETURN_QTY)"
        )

    def test_the_conversion_reports_nothing(self):
        # Nothing is lost, so nothing should be said. Before this the metric
        # carried TS-EXPR-UNRESOLVED and no portable sibling.
        assert _convert(MEASURE_ONLY).issues.as_dicts() == []

    def test_the_surfaced_attribute_is_unaffected(self):
        assert "Reason" in _fields(_convert(MEASURE_ONLY))

    def test_an_already_surfaced_operand_is_not_declared_twice(self):
        both = MEASURE_ONLY + [
            {"name": "Qty", "column_id": "RETURNS::Qty",
             "properties": {"column_type": "ATTRIBUTE"}},
        ]
        result = _convert(both)
        qty = [f for d in result.model["datasets"] for f in d.get("fields") or []
               if (f.get("label") or f["name"]) == "Qty"]
        assert len(qty) == 1


class TestTheDeclaredOperandStillProducesNoTML:
    """Same contract as a key's declared field: it exists to be named."""

    def test_the_column_is_still_stashed_as_unsurfaced(self):
        # The verbatim stash entry is what rebuilds the Table column, with its
        # properties and exact data_type. A field carries none of that, so
        # removing the entry would lose them.
        result = _convert(MEASURE_ONLY)
        dataset = result.model["datasets"][0]
        import json
        stash = json.loads(dataset["custom_extensions"][0]["data"])
        unsurfaced = [c.get("name") for c in stash.get("unsurfaced_columns") or []]
        assert "Qty" in unsurfaced
