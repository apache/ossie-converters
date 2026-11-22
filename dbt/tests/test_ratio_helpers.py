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

"""Generated-helper compatibility and conservative reverse-conversion contract."""

import json

import pytest

from metricflow_semantic_interfaces.implementations.element_config import PydanticSemanticLayerElementConfig
from metricflow_semantic_interfaces.implementations.metric import (
    PydanticConversionTypeParams,
    PydanticMetric,
    PydanticMetricInput,
    PydanticMetricTimeWindow,
    PydanticMetricTypeParams,
)
from metricflow_semantic_interfaces.implementations.semantic_manifest import PydanticSemanticManifest
from metricflow_semantic_interfaces.implementations.saved_query import PydanticSavedQuery, PydanticSavedQueryQueryParams
from metricflow_semantic_interfaces.type_enums import DataType, MetricType, TimeGranularity
from ossie_dbt.converter_issues import ConverterIssueType
from ossie_dbt.msi_to_ossie import MSIToOssieConverter
from ossie_dbt.ossie_to_msi import OssieToMSIConverter
from ossie_dbt.ratio_helpers import unchanged_ratio_helpers
from tests.helpers import _filter, _ossie_dataset, _ossie_doc, _ossie_field, _ossie_metric


def _generated():
    return (
        OssieToMSIConverter()
        .convert(
            _ossie_doc(
                datasets=[_ossie_dataset("orders", fields=[_ossie_field("amount"), _ossie_field("count")])],
                metrics=[_ossie_metric("avg", "SUM(amount) / COUNT(count)")],
            )
        )
        .output
    )


def _names(manifest):
    result = MSIToOssieConverter().convert(manifest)
    return {metric.name for metric in result.output.metrics or []}


def test_generated_public_contract_survives_manifest_serialization():
    manifest = PydanticSemanticManifest.parse_raw(_generated().json(by_alias=True, exclude_none=True))
    assert all(metric.type_params.is_private is False for metric in manifest.metrics)
    assert {metric.name for metric in manifest.metrics} == {"avg", "avg_numerator", "avg_denominator"}
    assert unchanged_ratio_helpers(manifest.metrics) == {"avg": ["avg_numerator", "avg_denominator"]}
    assert _names(manifest) == {"avg"}


@pytest.mark.parametrize(
    "marker",
    [
        None,
        {},
        [],
        "bad",
        1,
        {"version": 2},
        {"version": 1, "names": None},
        {"version": 1, "names": [None]},
        {"version": 1, "names": ["missing"]},
        {"version": 1, "names": ["avg", "avg_numerator"]},
        {"version": 1, "names": ["avg_numerator", "avg_numerator"]},
        {"version": 1, "names": ["avg_numerator", "avg_denominator"], "signature": []},
    ],
)
def test_missing_or_malformed_provenance_retains_public_helpers(marker):
    manifest = _generated()
    manifest.metrics[-1].config.meta["ossie"]["generated_ratio_helpers"] = marker
    assert _names(manifest) == {"avg", "avg_numerator", "avg_denominator"}


def test_legacy_unmarked_helpers_and_lookalike_names_are_not_inferred():
    manifest = _generated()
    manifest.metrics[-1].config = None
    assert _names(manifest) == {"avg", "avg_numerator", "avg_denominator"}


@pytest.mark.parametrize("change", ["expression", "filter", "alias", "offset", "datatype", "config", "private"])
def test_changed_definition_invalidates_provenance(change):
    manifest = _generated()
    helper, _, owner = manifest.metrics
    if change == "expression":
        helper.type_params.expr = "amount + 1"
    elif change == "filter":
        owner.type_params.numerator.filter = _filter("amount > 0")
    elif change == "alias":
        owner.type_params.numerator.alias = "changed_alias"
    elif change == "offset":
        owner.type_params.numerator.offset_window = PydanticMetricTimeWindow(count=1, granularity="month")
    elif change == "datatype":
        helper.datatype = DataType.DECIMAL
    elif change == "config":
        helper.config = PydanticSemanticLayerElementConfig(meta={"custom": "value"})
    elif change == "private":
        helper.type_params.is_private = True
    assert unchanged_ratio_helpers(manifest.metrics) == {}
    result = MSIToOssieConverter().convert(manifest)
    assert "avg_denominator" in {metric.name for metric in result.output.metrics or []}
    if change == "private":
        assert any(issue.issue_type is ConverterIssueType.PRIVATE_METRIC_DROPPED for issue in result.issues)
    if change == "offset":
        assert any(issue.issue_type is ConverterIssueType.NON_PORTABLE_METRIC_SEMANTICS for issue in result.issues)


@pytest.mark.parametrize("consumer", ["derived", "filter", "conversion"])
@pytest.mark.parametrize("private", [False, True])
def test_external_consumer_never_hides_helper_or_private_warning(consumer, private):
    manifest = _generated()
    manifest.metrics[0].type_params.is_private = private
    if consumer == "conversion":
        extra = PydanticMetric(
            name="conversion",
            type=MetricType.CONVERSION,
            type_params=PydanticMetricTypeParams(
                conversion_type_params=PydanticConversionTypeParams(
                    base_metric=PydanticMetricInput(name="avg_numerator"),
                    conversion_metric=PydanticMetricInput(name="avg_denominator"),
                    entity="order",
                )
            ),
        )
    else:
        extra = PydanticMetric(
            name="extra",
            type=MetricType.DERIVED,
            type_params=PydanticMetricTypeParams(
                expr="renamed + 1",
                metrics=[
                    PydanticMetricInput(name="avg_numerator" if consumer == "derived" else "avg", alias="renamed")
                ],
            ),
        )
        if consumer == "filter":
            extra.filter = _filter("{{ Metric('avg_numerator') }} > 0")
    manifest.metrics.append(extra)
    assert unchanged_ratio_helpers(manifest.metrics) == {}
    result = MSIToOssieConverter().convert(manifest)
    if private:
        assert any(
            issue.issue_type is ConverterIssueType.PRIVATE_METRIC_DROPPED and issue.element_name == "avg_numerator"
            for issue in result.issues
        )
    else:
        assert "avg_numerator" in {metric.name for metric in result.output.metrics or []}


def test_failed_owner_retains_helpers_even_with_original_marker():
    manifest = _generated()
    manifest.metrics[-1].type_params.numerator.offset_to_grain = TimeGranularity.MONTH
    assert _names(manifest) == {"avg_numerator", "avg_denominator"}


def test_input_manifest_is_not_mutated_by_provenance_inspection():
    manifest = _generated()
    before = json.loads(manifest.json())
    unchanged_ratio_helpers(manifest.metrics)
    assert json.loads(manifest.json()) == before


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(amount) / COUNT(count)",
        "(SUM(amount) / COUNT(count)) / SUM(amount)",
    ],
)
def test_nested_and_colliding_families_survive_repeated_json_round_trips(expression):
    doc = _ossie_doc(
        datasets=[_ossie_dataset("orders", fields=[_ossie_field("amount"), _ossie_field("count")])],
        metrics=[_ossie_metric("avg", expression), _ossie_metric("avg_numerator", "SUM(amount)")],
    )
    for _ in range(3):
        manifest = PydanticSemanticManifest.parse_raw(
            OssieToMSIConverter().convert(doc).output.json(by_alias=True, exclude_none=True)
        )
        assert all(metric.type_params.is_private is False for metric in manifest.metrics)
        result = MSIToOssieConverter().convert(manifest)
        assert result.issues == []
        assert {metric.name for metric in result.output.metrics} == {"avg", "avg_numerator"}
        doc = result.output


@pytest.mark.parametrize("reference", ["metrics", "filter"])
def test_saved_query_consumers_keep_generated_helpers(reference):
    manifest = _generated()
    params = PydanticSavedQueryQueryParams(metrics=["avg_numerator" if reference == "metrics" else "avg"])
    if reference == "filter":
        params.where = _filter("{{ Metric('avg_numerator') }} > 0")
    manifest.saved_queries.append(PydanticSavedQuery(name="saved", query_params=params))
    assert _names(manifest) == {"avg", "avg_numerator", "avg_denominator"}
