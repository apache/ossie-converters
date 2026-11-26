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

"""`AGG(source.measure)` is a COLUMN in ThoughtSpot, never a formula.

Core-spec Metric Scoping rule 7 lets a dataset-scoped metric compose a
measure of its `source` when that source is a measure relation. Rule 8 says
the call does not evaluate the measure where the metric is defined: it is
evaluated when a consuming query evaluates the metric, in that query's
context. Rule 9 forbids a consumer replacing `AGG` with an ordinary
aggregate.

ThoughtSpot's counterpart is a pre-aggregated measure column, handed to the
platform's own measure function. There is no formula syntax for it, so
`_build_metric`'s "always a formula" rule is the one shape that cannot
express this one, and the column is built directly instead.

A source measure is not a field -- "a field is never a measure" (core-spec,
Datasets) -- so nothing declares it and no column exists for it. One is
minted, typed from the metric's own `datatype`, the only place the document
states it. A metric that declares none is refused rather than typed by
guesswork, and so is one whose measure a field also declares, which the
spec forbids and which would otherwise put two model columns over one
table column.

Shape verified live (2026-11-26, simba-test) three ways: a Semantic
View-backed Table on that cluster carries exactly these four column
properties; a Model built in this shape imports; and AgentQL accepts
`AGG()` over the resulting column and returns a row. Without the flag a
newer build plans the column as an ATTRIBUTE and refuses `AGG()` with
`COLUMNS_NOT_GROUPED_OR_AGGREGATED` -- which is what makes ThoughtSpot a
conforming consumer of rule 9 rather than a silently wrong one.
"""
from ossie_thoughtspot.issues import IssueLog, Severity
from ossie_thoughtspot.ossie_to_thoughtspot import (
    _pre_aggregated_source_measure,
    build_table,
    convert,
)


def _dataset(metrics, *, fields=None):
    return {
        "name": "SV_SALES",
        "source": "SALES.PUBLIC.SV_SALES",
        "fields": fields or [{"name": "region", "expression": {"sql": "region"}}],
        "metrics": metrics,
    }


def _convert(metrics, **kw):
    return convert({"version": "0.3.0.dev0", "name": "sales",
                    "datasets": [_dataset(metrics, **kw)]})


AGG_REVENUE = [{"name": "Revenue", "description": "Dollar value.",
                "datatype": "Decimal",
                "expression": {"sql": "AGG(source.total_revenue)"}}]


class TestRecognisingTheCall:

    def test_a_single_source_measure_is_matched(self):
        assert _pre_aggregated_source_measure(AGG_REVENUE[0]) == "total_revenue"

    def test_the_call_name_is_matched_case_insensitively(self):
        metric = {"name": "m", "expression": {"sql": "agg(source.total_revenue)"}}
        assert _pre_aggregated_source_measure(metric) == "total_revenue"

    def test_the_source_qualifier_is_matched_case_insensitively(self):
        metric = {"name": "m", "expression": {"sql": "AGG(SOURCE.total_revenue)"}}
        assert _pre_aggregated_source_measure(metric) == "total_revenue"

    def test_an_ordinary_aggregate_is_not_matched(self):
        metric = {"name": "m", "expression": {"sql": "SUM(source.total_revenue)"}}
        assert _pre_aggregated_source_measure(metric) is None

    def test_agg_of_an_unqualified_name_is_not_matched(self):
        # Rule 7: the argument MUST be a single `source.measure` reference.
        metric = {"name": "m", "expression": {"sql": "AGG(total_revenue)"}}
        assert _pre_aggregated_source_measure(metric) is None

    def test_a_composite_is_not_matched(self):
        # Legal Ossie, but a pre-aggregated measure is a column and a
        # composite is not one.
        metric = {"name": "m", "expression": {
            "sql": "AGG(source.total_revenue) / COUNT(DISTINCT order_id)"}}
        assert _pre_aggregated_source_measure(metric) is None


class TestTheTableColumnIsMinted:

    def _columns(self, metrics=None, **kw):
        doc = build_table(_dataset(metrics or AGG_REVENUE, **kw), IssueLog())
        return {c["name"]: c for c in doc.body["columns"]}

    def test_a_column_is_added_for_the_measure(self):
        assert "total_revenue" in self._columns()

    def test_it_carries_all_four_properties(self):
        properties = self._columns()["total_revenue"]["properties"]
        assert properties["column_type"] == "MEASURE"
        assert properties["aggregation"] == "AGGREGATE"
        assert properties["index_type"] == "DONT_INDEX"
        assert properties["is_pre_aggregated_measure"] is True

    def test_its_datatype_comes_from_the_metric(self):
        column = self._columns()["total_revenue"]
        assert column["db_column_properties"]["data_type"] == "DOUBLE"
        assert column["db_column_name"] == "total_revenue"

    def test_nothing_is_minted_without_a_datatype(self):
        untyped = [{"name": "Revenue",
                    "expression": {"sql": "AGG(source.total_revenue)"}}]
        assert "total_revenue" not in self._columns(untyped)

    def test_an_ordinary_field_is_untouched(self):
        assert "is_pre_aggregated_measure" not in (
            self._columns()["region"].get("properties") or {})

    def test_a_dataset_with_no_agg_metric_mints_nothing(self):
        plain = [{"name": "m", "datatype": "Decimal",
                  "expression": {"sql": "SUM(region)"}}]
        assert list(self._columns(plain)) == ["region"]


class TestTheModelSurfacesItAsAColumn:

    def _model(self):
        return _convert(AGG_REVENUE).documents.model.body

    def test_it_is_a_column_not_a_formula(self):
        model = self._model()
        assert "formulas" not in model
        [entry] = [c for c in model["columns"] if c["name"] == "Revenue"]
        assert entry["column_id"] == "SV_SALES::total_revenue"

    def test_the_column_declares_the_aggregate_pair(self):
        [entry] = [c for c in self._model()["columns"] if c["name"] == "Revenue"]
        properties = entry["properties"]
        assert properties["column_type"] == "MEASURE"
        assert properties["aggregation"] == "AGGREGATE"
        assert properties["index_type"] == "DONT_INDEX"

    def test_the_metrics_description_is_carried(self):
        [entry] = [c for c in self._model()["columns"] if c["name"] == "Revenue"]
        assert entry["description"] == "Dollar value."

    def test_exactly_one_model_column_points_at_the_measure(self):
        # Two would mean a field declared it as well, which the spec forbids
        # and which I16 would then be violated by from the other side.
        pointing = [c["name"] for c in self._model()["columns"]
                    if c.get("column_id") == "SV_SALES::total_revenue"]
        assert pointing == ["Revenue"]

    def test_the_conversion_is_clean(self):
        codes = [i["code"] for i in _convert(AGG_REVENUE).issues.as_dicts()
                 if i["code"] != "TS-DATASET-CONNECTION-MISSING"]
        assert codes == []


class TestWhatIsRefused:

    def _codes(self, metrics, **kw):
        return [i["code"] for i in _convert(metrics, **kw).issues.as_dicts()]

    def test_a_metric_with_no_datatype_is_refused(self):
        untyped = [{"name": "Revenue",
                    "expression": {"sql": "AGG(source.total_revenue)"}}]
        assert "TS-METRIC-SOURCE-MEASURE-UNTYPED" in self._codes(untyped)

    def test_the_refusal_names_the_measure_and_the_remedy(self):
        untyped = [{"name": "Revenue",
                    "expression": {"sql": "AGG(source.total_revenue)"}}]
        [issue] = [i for i in _convert(untyped).issues.as_dicts()
                   if i["code"] == "TS-METRIC-SOURCE-MEASURE-UNTYPED"]
        assert issue["severity"] == Severity.ERROR.value
        assert "total_revenue" in issue["message"]
        assert "Give the metric a datatype" in issue["message"]
        assert issue["object_ref"] == "metric:Revenue"

    def test_a_measure_a_field_also_declares_is_refused(self):
        # "A field is never a measure". Building both would put two model
        # columns over one table column, one of them wrongly typed.
        codes = self._codes(AGG_REVENUE, fields=[
            {"name": "total_revenue", "expression": {"sql": "total_revenue"}}])
        assert "TS-METRIC-SOURCE-MEASURE-UNTYPED" in codes

    def test_a_refused_metric_leaves_the_model_importable(self):
        untyped = [{"name": "Revenue",
                    "expression": {"sql": "AGG(source.total_revenue)"}}]
        model = _convert(untyped).documents.model.body
        assert [c["name"] for c in model["columns"]] == ["region"]
