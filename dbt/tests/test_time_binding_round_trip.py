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

"""Round trip of the time spine and the aggregation time dimension (apache/ossie-converters#302)."""

import copy
import json
from typing import Any, Dict, List, Optional

import yaml
from metricflow_semantics.model.dbt_manifest_parser import parse_manifest_from_dbt_generated_manifest
from ossie import OssieSemanticModel
from ossie_dbt.converter_issues import ConverterIssueType
from ossie_dbt.msi_to_ossie import MSIToOssieConverter
from ossie_dbt.ossie_to_msi import OssieToMSIConverter

SPINE = {
    "node_relation": {"schema_name": "public", "alias": "calendar"},
    "primary_column": {"name": "date_day", "time_granularity": "day"},
}


def _simple_metric(name: str, agg_time_dimension: Optional[str], expr: str = "quantity") -> Dict[str, Any]:
    params: Dict[str, Any] = {"semantic_model": "orders", "agg": "sum"}
    if agg_time_dimension is not None:
        params["agg_time_dimension"] = agg_time_dimension
    return {"name": name, "type": "simple", "type_params": {"expr": expr, "metric_aggregation_params": params}}


def _ratio_metric() -> Dict[str, Any]:
    return {
        "name": "unit_price",
        "type": "ratio",
        "type_params": {"numerator": {"name": "revenue"}, "denominator": {"name": "units_sold"}},
    }


def _manifest(
    metrics: List[Dict[str, Any]],
    spines: Optional[List[Dict[str, Any]]] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    semantic_model: Dict[str, Any] = {
        "name": "orders",
        "node_relation": {"schema_name": "public", "alias": "orders"},
        "entities": [{"name": "order_id", "type": "primary", "expr": "order_id"}],
        "dimensions": [
            {"name": "ordered_on", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "shipped_on", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "quantity", "type": "categorical"},
            {"name": "price", "type": "categorical"},
        ],
    }
    if defaults is not None:
        semantic_model["defaults"] = defaults
    return {
        "semantic_models": [semantic_model],
        "metrics": metrics,
        "project_configuration": {"time_spines": spines if spines is not None else [SPINE]},
    }


def _ossie_document(to_ossie_output: OssieSemanticModel) -> Dict[str, Any]:
    return yaml.safe_load(to_ossie_output.to_ossie_yaml())


def _round_trip(manifest: Dict[str, Any]):
    parsed = parse_manifest_from_dbt_generated_manifest(json.dumps(manifest))
    to_ossie = MSIToOssieConverter().convert(parsed)
    # Go through YAML, as the CLI does.
    document = OssieSemanticModel.model_validate(_ossie_document(to_ossie.output))
    to_msi = OssieToMSIConverter().convert(document)
    return to_ossie, to_msi


def _issue_types(result) -> List[ConverterIssueType]:
    return [issue.issue_type for issue in result.issues]


def _metric(manifest, name: str):
    return next(m for m in manifest.metrics if m.name == name)


def _binding(manifest, name: str) -> Optional[str]:
    return _metric(manifest, name).type_params.metric_aggregation_params.agg_time_dimension


class TestTimeSpineRoundTrip:
    def test_time_spine_survives(self):
        _, to_msi = _round_trip(_manifest([_simple_metric("units_sold", "ordered_on")]))
        spines = to_msi.output.project_configuration.time_spines
        assert len(spines) == 1
        assert spines[0].node_relation.alias == "calendar"
        assert spines[0].primary_column.name == "date_day"

    def test_no_spine_in_source_is_reported(self):
        _, to_msi = _round_trip(_manifest([_simple_metric("units_sold", "ordered_on")], spines=[]))
        assert to_msi.output.project_configuration.time_spines == []
        assert ConverterIssueType.TIME_SPINE_LOSS in _issue_types(to_msi)

    def test_spine_present_is_not_reported(self):
        to_ossie, to_msi = _round_trip(_manifest([_simple_metric("units_sold", "ordered_on")]))
        assert ConverterIssueType.TIME_SPINE_LOSS not in _issue_types(to_ossie)
        assert ConverterIssueType.TIME_SPINE_LOSS not in _issue_types(to_msi)


class TestAggTimeDimensionRoundTrip:
    def test_binding_survives_and_is_not_guessed(self):
        _, to_msi = _round_trip(_manifest([_simple_metric("units_sold", "ordered_on")]))
        assert _binding(to_msi.output, "units_sold") == "ordered_on"
        assert ConverterIssueType.AGG_TIME_DIMENSION_LOSS not in _issue_types(to_msi)

    def test_each_metric_keeps_its_own_binding(self):
        metrics = [_simple_metric("by_order", "ordered_on"), _simple_metric("by_ship", "shipped_on", "price")]
        _, to_msi = _round_trip(_manifest(metrics))
        assert _binding(to_msi.output, "by_order") == "ordered_on"
        assert _binding(to_msi.output, "by_ship") == "shipped_on"

    def test_semantic_model_default_survives(self):
        manifest = _manifest([_simple_metric("units_sold", None)], defaults={"agg_time_dimension": "shipped_on"})
        _, to_msi = _round_trip(manifest)
        assert to_msi.output.semantic_models[0].defaults.agg_time_dimension == "shipped_on"
        # The default covers the metric, so nothing is lost.
        assert ConverterIssueType.AGG_TIME_DIMENSION_LOSS not in _issue_types(to_msi)

    def test_missing_binding_is_reported_not_guessed(self):
        _, to_msi = _round_trip(_manifest([_simple_metric("units_sold", None)]))
        assert _binding(to_msi.output, "units_sold") is None
        issues = [i for i in to_msi.issues if i.issue_type is ConverterIssueType.AGG_TIME_DIMENSION_LOSS]
        assert [i.element_name for i in issues] == ["units_sold"]

    def test_ratio_sub_metrics_inherit_the_shared_binding(self):
        metrics = [
            _simple_metric("revenue", "shipped_on", "price"),
            _simple_metric("units_sold", "shipped_on"),
            _ratio_metric(),
        ]
        _, to_msi = _round_trip(_manifest(metrics))
        simple = [m for m in to_msi.output.metrics if m.type_params.metric_aggregation_params is not None]
        assert simple
        assert {m.type_params.metric_aggregation_params.agg_time_dimension for m in simple} == {"shipped_on"}
        assert ConverterIssueType.AGG_TIME_DIMENSION_LOSS not in _issue_types(to_msi)

    def test_ratio_with_conflicting_bindings_is_reported(self):
        metrics = [
            _simple_metric("revenue", "shipped_on", "price"),
            _simple_metric("units_sold", "ordered_on"),
            _ratio_metric(),
        ]
        to_ossie, _ = _round_trip(_manifest(metrics))
        assert ConverterIssueType.AGG_TIME_DIMENSION_LOSS in _issue_types(to_ossie)


class TestExtensionsDoNotDisturbMeta:
    def test_meta_and_time_binding_coexist(self):
        metric = _simple_metric("units_sold", "ordered_on")
        metric["config"] = {"meta": {"owner": "finance"}}
        _, to_msi = _round_trip(_manifest([metric]))
        out = _metric(to_msi.output, "units_sold")
        assert out.config.meta == {"owner": "finance"}
        assert out.type_params.metric_aggregation_params.agg_time_dimension == "ordered_on"
        assert not any(
            i.issue_type
            in (ConverterIssueType.DUPLICATE_DBT_CUSTOM_EXTENSION, ConverterIssueType.INVALID_DBT_CUSTOM_EXTENSION)
            for i in to_msi.issues
        )

    def test_invalid_time_spine_extension_is_reported(self):
        to_ossie, _ = _round_trip(_manifest([_simple_metric("units_sold", "ordered_on")]))
        document = copy.deepcopy(_ossie_document(to_ossie.output))
        document["nonportable_extensions"] = [{"vendor_name": "DBT", "data": {"time_spines": "nope"}}]
        result = OssieToMSIConverter().convert(OssieSemanticModel.model_validate(document))
        assert ConverterIssueType.INVALID_DBT_CUSTOM_EXTENSION in _issue_types(result)
