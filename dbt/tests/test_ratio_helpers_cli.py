# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""Regression coverage for COUNT provenance through the actual dbt CLI parser."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import yaml

from metricflow_semantics.model.dbt_manifest_parser import parse_manifest_from_dbt_generated_manifest
from metricflow_semantic_interfaces.implementations.element_config import PydanticSemanticLayerElementConfig
from metricflow_semantic_interfaces.implementations.metric import (
    PydanticMetric, PydanticMetricInput, PydanticMetricTimeWindow, PydanticMetricTypeParams,
)
from metricflow_semantic_interfaces.implementations.saved_query import PydanticSavedQuery, PydanticSavedQueryQueryParams
from metricflow_semantic_interfaces.type_enums import AggregationType, DataType, MetricType
from ossie import OssieCustomExtension, OssieVendor
from ossie_dbt.converter_issues import ConverterIssueType
from ossie_dbt.msi_to_ossie import MSIToOssieConverter
from ossie_dbt.ossie_to_msi import OssieToMSIConverter
from ossie_dbt.ratio_helpers import _definition, unchanged_ratio_helpers
from tests.helpers import _filter, _ossie_dataset, _ossie_doc, _ossie_field, _ossie_metric

KEY = "generated_ratio_helpers"
USER_META = {"team": "finance", "ossie": {"owner": "accounting"}}
EXPRESSIONS = [
    "SUM(orders.amount) / COUNT(orders.order_id)",
    "SUM(orders.amount) / COUNT(DISTINCT orders.order_id)",
    "SUM(orders.amount) / COUNT(*)",
    "(SUM(orders.amount) / COUNT(orders.order_id)) / SUM(orders.amount)",
    "SUM(orders.amount) / COUNT(1)",
    "SUM(orders.amount) / COUNT(orders.*)",
    "(SUM(orders.amount) / COUNT(DISTINCT orders.order_id)) / SUM(orders.amount)",
    "(SUM(orders.amount) / COUNT(*)) / SUM(orders.amount)",
]
IDS = ["count_column", "count_distinct", "count_star", "nested_count", "count_one", "qualified_star", "nested_distinct", "nested_star"]


def _document(expression=EXPRESSIONS[0], collision=False):
    extension = OssieCustomExtension(vendor_name=OssieVendor.DBT.value, data=json.dumps({"meta": USER_META}))
    metrics = [_ossie_metric("avg", expression, custom_extensions=[extension])]
    if collision:
        metrics.append(_ossie_metric("avg_numerator", "SUM(orders.amount)"))
    return _ossie_doc(
        datasets=[_ossie_dataset("orders", fields=[_ossie_field("amount"), _ossie_field("order_id")])],
        metrics=metrics,
    )


def _manifest():
    return OssieToMSIConverter().convert(_document()).output


def _parse(manifest):
    return parse_manifest_from_dbt_generated_manifest(manifest.json(by_alias=True, exclude_none=True))


def _cli(command: str, source: Path, target: Path):
    result = subprocess.run(
        [sys.executable, "-m", "ossie_dbt.cli", command, "-i", str(source), "-o", str(target)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _evaluate(sql, rows):
    # Qualified COUNT(orders.*) is a supported Ossie spelling, not SQLite syntax.
    sql = sql.replace("COUNT(orders.*)", "COUNT(*)")
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE orders (amount REAL, order_id INTEGER)")
        connection.executemany("INSERT INTO orders VALUES (?, ?)", rows)
        return connection.execute(f"SELECT {sql} FROM orders").fetchone()[0]


@pytest.mark.parametrize("expression", EXPRESSIONS, ids=IDS)
def test_real_cli_three_round_trips_do_not_grow_helpers(tmp_path, expression):
    source = tmp_path / "input.yaml"
    source.write_text(_document(expression).to_ossie_yaml(), encoding="utf-8")
    observed = []
    row_sets = [[(12.0, 1), (7.0, 1), (3.0, None), (2.0, 2)], [(2.0, None), (3.0, None)], []]
    for round_number in range(3):
        manifest_path = tmp_path / f"manifest-{round_number}.json"
        target = tmp_path / f"round-{round_number}.yaml"
        before = source.read_bytes()
        forward = _cli("ossie-to-msi", source, manifest_path)
        assert source.read_bytes() == before
        assert "[WARNING]" not in forward.stderr
        raw = json.loads(manifest_path.read_text())
        assert all(not m["type_params"].get("is_private", False) for m in raw["metrics"])
        reverse = _cli("msi-to-ossie", manifest_path, target)
        assert "[WARNING]" not in reverse.stderr
        output = yaml.safe_load(target.read_text())
        observed.append([m["name"] for m in output["metrics"]])
        metric = next(m for m in output["metrics"] if m["name"] == "avg")
        meta = json.loads(metric["custom_extensions"][0]["data"])["meta"]
        assert meta["team"] == USER_META["team"]
        assert meta["ossie"]["owner"] == USER_META["ossie"]["owner"]
        sql = metric["expression"]["dialects"][0]["expression"]
        for rows in row_sets:
            expected = _evaluate(expression, rows)
            actual = _evaluate(sql, rows)
            assert actual is None if expected is None else actual == pytest.approx(expected)
        source = target
    assert observed == [["avg"], ["avg"], ["avg"]], observed


def test_real_cli_keeps_user_metric_with_a_helper_like_name(tmp_path):
    source = tmp_path / "input.yaml"
    source.write_text(_document(collision=True).to_ossie_yaml())
    for i in range(3):
        manifest, target = tmp_path / f"m{i}.json", tmp_path / f"o{i}.yaml"
        _cli("ossie-to-msi", source, manifest)
        _cli("msi-to-ossie", manifest, target)
        assert {m["name"] for m in yaml.safe_load(target.read_text())["metrics"]} == {"avg", "avg_numerator"}
        source = target


def test_normalized_signature_accepts_raw_parsed_and_reparsed_without_mutation():
    raw = _manifest()
    before = raw.json()
    assert raw.metrics[-1].config.meta["ossie"][KEY]["version"] == 2
    assert next(m for m in raw.metrics if m.name == "avg_denominator").type_params.metric_aggregation_params.agg == AggregationType.COUNT
    for manifest in (raw, _parse(raw), _parse(_parse(raw))):
        snapshot = manifest.json()
        assert unchanged_ratio_helpers(manifest.metrics) == {"avg": ["avg_numerator", "avg_denominator"]}
        assert manifest.json() == snapshot
        assert {m.name for m in MSIToOssieConverter().convert(manifest).output.metrics} == {"avg"}
    assert raw.json() == before


@pytest.mark.parametrize("change", ["expression", "unsafe_sum", "count_distinct", "filter", "alias", "offset", "datatype", "config", "private"])
@pytest.mark.parametrize("already_parsed", [False, True])
def test_edited_definitions_remain_conservative_across_cli_parser(change, already_parsed):
    manifest = _manifest()
    if already_parsed:
        manifest = _parse(manifest)
    numerator, denominator, owner = manifest.metrics
    if change == "expression":
        denominator.type_params.expr = "amount"
    elif change == "unsafe_sum":
        denominator.type_params.metric_aggregation_params.agg = AggregationType.SUM
        denominator.type_params.expr = "order_id"
    elif change == "count_distinct":
        denominator.type_params.metric_aggregation_params.agg = AggregationType.COUNT_DISTINCT
        denominator.type_params.expr = "order_id"
    elif change == "filter":
        owner.type_params.numerator.filter = _filter("amount > 0")
    elif change == "alias":
        owner.type_params.numerator.alias = "changed_alias"
    elif change == "offset":
        owner.type_params.numerator.offset_window = PydanticMetricTimeWindow(count=1, granularity="month")
    elif change == "datatype":
        numerator.datatype = DataType.DECIMAL
    elif change == "config":
        numerator.config = PydanticSemanticLayerElementConfig(meta={"custom": "changed"})
    elif change == "private":
        numerator.type_params.is_private = True
    parsed = _parse(manifest)
    assert unchanged_ratio_helpers(parsed.metrics) == {}
    result = MSIToOssieConverter().convert(parsed)
    assert "avg_denominator" in {m.name for m in result.output.metrics}
    if change == "private":
        assert any(i.issue_type == ConverterIssueType.PRIVATE_METRIC_DROPPED for i in result.issues)


@pytest.mark.parametrize("consumer", ["derived", "metric_filter", "saved_metric", "saved_filter"])
def test_external_consumers_still_keep_helpers_after_cli_parsing(consumer):
    manifest = _manifest()
    if consumer.startswith("saved"):
        params = PydanticSavedQueryQueryParams(metrics=["avg_numerator" if consumer == "saved_metric" else "avg"])
        if consumer == "saved_filter":
            params.where = _filter("{{ Metric('avg_numerator') }} > 0")
        manifest.saved_queries.append(PydanticSavedQuery(name="saved", query_params=params))
    else:
        extra = PydanticMetric(name="extra", type=MetricType.DERIVED,
            type_params=PydanticMetricTypeParams(expr="m + 1", metrics=[PydanticMetricInput(name="avg_numerator" if consumer == "derived" else "avg", alias="m")]))
        if consumer == "metric_filter":
            extra.filter = _filter("{{ Metric('avg_numerator') }} > 0")
        manifest.metrics.append(extra)
    parsed = _parse(manifest)
    assert unchanged_ratio_helpers(parsed.metrics, parsed.saved_queries) == {}
    assert "avg_numerator" in {m.name for m in MSIToOssieConverter().convert(parsed).output.metrics}


@pytest.mark.parametrize("marker", [None, {}, {"version": 3}, {"version": 2, "names": ["missing"], "signature": "bad"}])
def test_cli_parser_does_not_infer_missing_or_invalid_provenance(marker):
    manifest = _manifest()
    manifest.metrics[-1].config.meta["ossie"][KEY] = marker
    result = MSIToOssieConverter().convert(_parse(manifest))
    assert {m.name for m in result.output.metrics} == {"avg", "avg_numerator", "avg_denominator"}


def test_version_one_only_folds_when_exact_legacy_definitions_are_available():
    manifest = _manifest()
    definitions = [_definition(m) for m in sorted(manifest.metrics, key=lambda m: m.name)]
    marker = manifest.metrics[-1].config.meta["ossie"][KEY]
    marker["version"] = 1
    marker["signature"] = hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()
    assert unchanged_ratio_helpers(manifest.metrics) == {"avg": ["avg_numerator", "avg_denominator"]}
    parsed = _parse(manifest)
    assert unchanged_ratio_helpers(parsed.metrics) == {}
    assert {m.name for m in MSIToOssieConverter().convert(parsed).output.metrics} == {"avg", "avg_numerator", "avg_denominator"}


def test_invalid_count_expression_cannot_establish_helper_ownership():
    manifest = _manifest()
    manifest.metrics[1].type_params.expr = None
    assert unchanged_ratio_helpers(manifest.metrics) == {}


def test_actual_cli_keeps_private_metric_warning(tmp_path):
    manifest = _manifest()
    manifest.metrics[0].type_params.is_private = True
    source, target = tmp_path / "private.json", tmp_path / "private.yaml"
    source.write_text(manifest.json(by_alias=True, exclude_none=True))
    result = _cli("msi-to-ossie", source, target)
    assert "PRIVATE_METRIC_DROPPED" in result.stderr
    assert "avg_numerator" in result.stderr
    assert "avg_denominator" in {m["name"] for m in yaml.safe_load(target.read_text())["metrics"]}


@pytest.mark.parametrize("expression", [
    "SUM(CASE WHEN orders.amount > 0 THEN 1 ELSE 0 END) / COUNT(orders.order_id)",
    "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY orders.amount) / COUNT(orders.order_id)",
    "(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY orders.amount) / COUNT(orders.order_id)) / SUM(orders.amount)",
])
def test_other_generated_aggregations_survive_real_cli_parser(tmp_path, expression):
    source = tmp_path / "input.yaml"
    source.write_text(_document(expression).to_ossie_yaml())
    for iteration in range(3):
        manifest_path = tmp_path / f"aggregation-{iteration}.json"
        target = tmp_path / f"aggregation-{iteration}.yaml"
        assert "[WARNING]" not in _cli("ossie-to-msi", source, manifest_path).stderr
        assert "[WARNING]" not in _cli("msi-to-ossie", manifest_path, target).stderr
        assert [m["name"] for m in yaml.safe_load(target.read_text())["metrics"]] == ["avg"]
        source = target
