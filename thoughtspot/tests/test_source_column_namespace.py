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

"""A column of a dataset's `source` is not one of its fields.

apache/ossie#486 defines three reference namespaces. `_build_field_index`
serves one of them, keyed by Ossie `dataset.field`. The other two name
columns of the dataset's `source` instead, which is a different set of
names: a dataset may declare the field `cust` over the source column
`customer_name`, and then the two indexes agree about nothing.

* A dataset-scoped metric qualifies a source column as `source.column`
  (Metric Scoping, rule 2), case insensitively (rule 2), unless the dataset
  declares a field actually called `source` (rule 3).
* A field expression's identifiers name source columns directly, unqualified
  (Name Resolution).

The index is built from the already-built Table and SQL View documents,
where `db_column_name` and `sql_output_column` state the source column
outright, rather than re-derived from the fields -- `build_table` has
already classified every one of them against the real log.

Tracked as apache/ossie-converters#101.
"""
from ossie_thoughtspot.constants import (
    FIELD_STASH_DB_COLUMN_NAME,
    FIELD_STASH_DB_COLUMN_NAME_WITNESS,
)
from ossie_thoughtspot.issues import IssueLog, Severity
from ossie_thoughtspot.ossie_to_thoughtspot import (
    _build_source_column_index,
    _dataset_scoped_resolver,
    _field_expression_resolver,
    _translate_ansi_sql,
    build_table,
    convert,
)

SRC = {"orders.CUSTOMER_NAME": ("orders", "Cust"), "orders.AMT": ("orders", "Amount")}
FLD = {"orders.cust": ("orders", "Cust"), "orders.amount": ("orders", "Amount")}


def _document(fields, *, metrics=None):
    dataset = {"name": "orders", "source": "SALES.PUBLIC.ORDERS", "fields": fields}
    if metrics is not None:
        dataset["metrics"] = metrics
    return {"version": "0.3.0.dev0", "name": "sales", "datasets": [dataset]}


def _table_for(fields):
    return build_table(_document(fields)["datasets"][0], IssueLog())


def _ts_field(name, display, warehouse):
    """A round-tripped field: display name and warehouse name genuinely differ."""
    return {
        "name": name,
        "expression": {"dialects": [
            {"dialect": "THOUGHTSPOT", "expression": f"[ORDERS::{display}]"}]},
        "nonportable_extensions": [{"vendor_name": "THOUGHTSPOT", "data": {
            "_v": 1,
            FIELD_STASH_DB_COLUMN_NAME: warehouse,
            FIELD_STASH_DB_COLUMN_NAME_WITNESS: display,
        }}],
    }


class TestTheIndexIsKeyedByTheSourceColumn:

    def test_a_table_column_is_keyed_by_its_db_column_name(self):
        index = _build_source_column_index({"orders": _table_for(
            [_ts_field("cust", "Cust", "CUSTOMER_NAME")])})
        assert index["orders.CUSTOMER_NAME"] == ("orders", "Cust")

    def test_the_field_name_is_not_a_key(self):
        # The whole point: `cust` is the field, `CUSTOMER_NAME` is the column.
        index = _build_source_column_index({"orders": _table_for(
            [_ts_field("cust", "Cust", "CUSTOMER_NAME")])})
        assert "orders.cust" not in index

    def test_a_dataset_with_no_built_document_contributes_nothing(self):
        assert _build_source_column_index({"orders": None}) == {}


class TestADatasetScopedMetricResolvesSourceColumns:

    def _resolve(self, key, *, field_index=None):
        resolver = _dataset_scoped_resolver(field_index or FLD, "orders", SRC)
        return resolver(key)

    def test_a_source_qualified_column_resolves(self):
        assert self._resolve("source.CUSTOMER_NAME") == ("orders", "Cust")

    def test_the_qualifier_is_case_insensitive(self):
        # Rule 2: `source.quantity` and `SOURCE.quantity` name the same column.
        assert self._resolve("SOURCE.CUSTOMER_NAME") == ("orders", "Cust")
        assert self._resolve("Source.CUSTOMER_NAME") == ("orders", "Cust")

    def test_an_unqualified_name_is_still_a_field_not_a_source_column(self):
        assert self._resolve("cust") == ("orders", "Cust")
        assert self._resolve("CUSTOMER_NAME") is None

    def test_another_datasets_field_still_does_not_resolve(self):
        # Rule 1, unchanged by the source namespace arriving beside it.
        assert self._resolve("returns.amount") is None

    def test_a_field_named_source_shadows_the_qualifier(self):
        # Rule 3: `source` is not reserved. A dataset that declares a field
        # called `source` cannot name source columns at all.
        shadowed = dict(FLD, **{"orders.source": ("orders", "Source")})
        assert self._resolve("source.CUSTOMER_NAME", field_index=shadowed) is None

    def test_a_source_column_that_does_not_exist_resolves_to_nothing(self):
        assert self._resolve("source.NOPE") is None


class TestAFieldExpressionResolvesSourceColumns:

    def _resolve(self, key):
        return _field_expression_resolver(SRC, FLD, "orders")(key)

    def test_an_unqualified_name_is_a_source_column(self):
        assert self._resolve("CUSTOMER_NAME") == ("orders", "Cust")

    def test_a_declared_field_is_accepted_after_it(self):
        # Not what the specification says a field expression contains, kept
        # because every field expression resolved this way before the source
        # namespace existed here.
        assert self._resolve("cust") == ("orders", "Cust")

    def test_the_source_namespace_wins_when_both_could_match(self):
        src = {"orders.x": ("orders", "FromSource")}
        fld = {"orders.x": ("orders", "FromField")}
        assert _field_expression_resolver(src, fld, "orders")("x") == (
            "orders", "FromSource")

    def test_a_bare_source_column_translates_to_a_column_reference(self):
        log = IssueLog()
        out = _translate_ansi_sql(
            "CUSTOMER_NAME", _field_expression_resolver(SRC, FLD, "orders"),
            log, object_ref="field:f",
        )
        assert out == "[orders::Cust]"
        assert log.as_dicts() == []


class TestEndToEnd:

    def test_a_dataset_metric_over_a_source_column_becomes_a_formula(self):
        document = _document(
            [_ts_field("cust", "Cust", "CUSTOMER_NAME"),
             _ts_field("amount", "Amount", "AMT")],
            metrics=[{"name": "total", "expression": {"sql": "SUM(source.AMT)"}}],
        )
        result = convert(document)
        formulas = {f["name"]: f["expr"]
                    for f in result.documents.model.body.get("formulas") or []}
        assert formulas["total"] == "sum ( [orders::Amount] )"

    def test_the_same_metric_by_field_name_also_works(self):
        document = _document(
            [_ts_field("amount", "Amount", "AMT")],
            metrics=[{"name": "total", "expression": {"sql": "SUM(amount)"}}],
        )
        result = convert(document)
        formulas = {f["name"]: f["expr"]
                    for f in result.documents.model.body.get("formulas") or []}
        assert formulas["total"] == "sum ( [orders::Amount] )"


class TestAnUndeclaredSourceColumnIsRefusedNotInvented:
    """A source column the dataset never declares has no datatype anywhere.

    The document states one only for a declared field, and the warehouse
    cannot be asked: `connection/search` returns an empty column hierarchy
    (live-verified, recorded in this repo's ts-cli rules). Minting a table
    column would therefore mean asserting a `data_type` nobody knows, and a
    column typed wrongly is the silent kind of failure this converter exists
    to avoid. So the metric is refused, and the issue says why.
    """

    def _issues(self, metric_sql):
        document = _document(
            [_ts_field("amount", "Amount", "AMT")],
            metrics=[{"name": "qty", "expression": {"sql": metric_sql}}],
        )
        return convert(document)

    def test_the_metric_is_not_built(self):
        result = self._issues("SUM(source.QUANTITY)")
        assert "formulas" not in result.documents.model.body

    def test_the_issue_names_the_column_the_dataset_and_the_remedy(self):
        result = self._issues("SUM(source.QUANTITY)")
        [issue] = [i for i in result.issues.as_dicts()
                   if i["code"] == "TS-METRIC-SOURCE-COLUMN-UNDECLARED"]
        assert issue["severity"] == Severity.ERROR.value
        assert "QUANTITY" in issue["message"]
        assert "orders" in issue["message"]
        assert "Declare it as a field" in issue["message"]
        assert issue["object_ref"] == "metric:qty"

    def test_a_declared_source_column_raises_nothing(self):
        result = self._issues("SUM(source.AMT)")
        assert [i for i in result.issues.as_dicts()
                if i["code"] == "TS-METRIC-SOURCE-COLUMN-UNDECLARED"] == []

    def test_one_refused_metric_does_not_take_the_others_with_it(self):
        document = _document(
            [_ts_field("amount", "Amount", "AMT")],
            metrics=[
                {"name": "qty", "expression": {"sql": "SUM(source.QUANTITY)"}},
                {"name": "tot", "expression": {"sql": "SUM(source.AMT)"}},
            ],
        )
        result = convert(document)
        built = [f["name"] for f in result.documents.model.body.get("formulas") or []]
        assert built == ["tot"]
