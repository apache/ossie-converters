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
"""PR #121/#145 integration: user metadata and generated provenance coexist."""
from copy import deepcopy
import json
import sqlite3
import pytest
import sqlglot
from sqlglot import exp
from ossie import OssieCustomExtension, OssieDataType, OssieDocument, OssieVendor
from metricflow_semantic_interfaces.implementations.semantic_manifest import PydanticSemanticManifest
from metricflow_semantic_interfaces.implementations.saved_query import PydanticSavedQuery, PydanticSavedQueryQueryParams
from ossie_dbt.converter_issues import ConverterIssueType
from ossie_dbt.msi_to_ossie import MSIToOssieConverter
from ossie_dbt.ossie_to_msi import OssieToMSIConverter
from ossie_dbt.ratio_helpers import unchanged_ratio_helpers
from tests.helpers import _ossie_dataset, _ossie_doc, _ossie_field, _ossie_metric

KEY = "generated_ratio_helpers"
USER_META = {"team": "finance", "labels": ["reporting"], "ossie": {"owner": "accounting"}}

def _document(meta=None, expression="SUM(orders.amount) / COUNT(orders.order_id)"):
    payload = deepcopy(USER_META if meta is None else meta)
    extension = OssieCustomExtension(vendor_name=OssieVendor.DBT.value, data=json.dumps({"meta": payload}))
    return _ossie_doc(
        datasets=[_ossie_dataset("orders", fields=[_ossie_field("amount"), _ossie_field("order_id")])],
        metrics=[_ossie_metric("avg", expression, custom_extensions=[extension])],
    )

def _user_metadata(meta):
    result = deepcopy(meta)
    namespace = result.get("ossie")
    if isinstance(namespace, dict):
        namespace.pop(KEY, None)
        if not namespace:
            result.pop("ossie")
    return result

def _canonical_sql(expression):
    # These fixtures deliberately contain one dataset. Normalize only that
    # qualifier and SQL parser spelling, not the expression's operations.
    tree = sqlglot.parse_one(expression)
    for column in tree.find_all(exp.Column):
        assert column.table in ("", "orders")
        column.set("table", None)
    return tree.sql()

def _evaluate(expression):
    # Includes a NULL key and a repeated key, so COUNT and COUNT(DISTINCT)
    # cannot accidentally be substituted for one another.
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE orders (amount REAL, order_id INTEGER)")
        connection.executemany("INSERT INTO orders VALUES (?, ?)",
                               [(12.0, 1), (7.0, 1), (3.0, None), (2.0, 2)])
        return connection.execute(f"SELECT {expression} FROM orders").fetchone()[0]

@pytest.mark.parametrize("expression", [
    "SUM(orders.amount) / COUNT(orders.order_id)",
    "(SUM(orders.amount) / COUNT(orders.order_id)) / SUM(orders.amount)",
    "SUM(orders.amount) / COUNT(DISTINCT orders.order_id)",
    "(SUM(orders.amount) / COUNT(DISTINCT orders.order_id)) / SUM(orders.amount)",
])
def test_metadata_and_ratio_remain_stable_across_three_serialized_round_trips(expression):
    doc = _document(expression=expression)
    normalized_expression = None
    previous_sql = None
    expected_value = _evaluate(expression)
    for _ in range(3):
        doc_before = doc.model_dump_json()
        forward = OssieToMSIConverter().convert(doc)
        assert doc.model_dump_json() == doc_before
        assert forward.issues == []
        manifest = PydanticSemanticManifest.parse_raw(forward.output.json(by_alias=True, exclude_none=True))
        owner = next(m for m in manifest.metrics if m.name == "avg")
        assert _user_metadata(owner.config.meta) == USER_META
        assert all(m.type_params.is_private is False for m in manifest.metrics)
        assert all(m.config is None for m in manifest.metrics if m.name != "avg")
        assert set(unchanged_ratio_helpers(manifest.metrics)) == {"avg"}
        reverse = MSIToOssieConverter().convert(manifest)
        assert reverse.issues == []
        assert [m.name for m in reverse.output.metrics] == ["avg"]
        metric = reverse.output.metrics[0]
        extensions = [e for e in metric.custom_extensions if e.vendor_name == OssieVendor.DBT.value]
        assert len(extensions) == 1
        assert _user_metadata(json.loads(extensions[0].data)["meta"]) == USER_META
        current_sql = metric.expression.dialects[0].expression
        assert _evaluate(current_sql) == pytest.approx(expected_value)
        current_expression = _canonical_sql(current_sql)
        if normalized_expression is not None:
            assert current_expression == normalized_expression
            if "COUNT(DISTINCT" in expression:
                assert current_sql == previous_sql
        normalized_expression = current_expression
        previous_sql = current_sql
        doc = OssieDocument.model_validate_json(reverse.output.model_dump_json())

def test_editing_restored_user_metadata_invalidates_the_original_signature():
    manifest = OssieToMSIConverter().convert(_document()).output
    owner = next(m for m in manifest.metrics if m.name == "avg")
    owner.config.meta["team"] = "operations"
    assert unchanged_ratio_helpers(manifest.metrics) == {}
    reverse = MSIToOssieConverter().convert(manifest)
    assert {m.name for m in reverse.output.metrics} == {"avg", "avg_numerator", "avg_denominator"}
    metric = next(m for m in reverse.output.metrics if m.name == "avg")
    assert json.loads(metric.custom_extensions[0].data)["meta"]["team"] == "operations"

def test_saved_query_reference_keeps_helpers_with_restored_user_metadata():
    manifest = OssieToMSIConverter().convert(_document()).output
    manifest.saved_queries.append(PydanticSavedQuery(
        name="helper_query", query_params=PydanticSavedQueryQueryParams(metrics=["avg_numerator"])))
    reverse = MSIToOssieConverter().convert(manifest)
    assert {m.name for m in reverse.output.metrics} == {"avg", "avg_numerator", "avg_denominator"}
    owner = next(m for m in reverse.output.metrics if m.name == "avg")
    assert _user_metadata(json.loads(owner.custom_extensions[0].data)["meta"]) == USER_META

@pytest.mark.parametrize("namespace", ["custom", ["custom"], None])
def test_non_object_user_namespace_is_preserved_without_guessing_helper_ownership(namespace):
    meta = {"team": "finance", "ossie": namespace}
    forward = OssieToMSIConverter().convert(_document(meta=meta))
    assert forward.issues == []
    owner = next(m for m in forward.output.metrics if m.name == "avg")
    assert owner.config.meta == meta
    assert unchanged_ratio_helpers(forward.output.metrics) == {}
    reverse = MSIToOssieConverter().convert(forward.output)
    assert {m.name for m in reverse.output.metrics} == {"avg", "avg_numerator", "avg_denominator"}

def test_invalid_extension_warning_survives_ratio_generation():
    doc = _document()
    doc.metrics[0].custom_extensions.insert(0, OssieCustomExtension(vendor_name=OssieVendor.DBT.value, data="{bad"))
    result = OssieToMSIConverter().convert(doc)
    assert [(i.issue_type, i.element_name) for i in result.issues] == [(ConverterIssueType.INVALID_DBT_CUSTOM_EXTENSION, "avg")]
    owner = next(m for m in result.output.metrics if m.name == "avg")
    assert _user_metadata(owner.config.meta) == USER_META

def test_duplicate_extension_warning_keeps_the_first_user_metadata():
    doc = _document()
    doc.metrics[0].custom_extensions.append(OssieCustomExtension(vendor_name=OssieVendor.DBT.value, data=json.dumps({"meta": {"team": "other"}})))
    result = OssieToMSIConverter().convert(doc)
    assert [(i.issue_type, i.element_name) for i in result.issues] == [(ConverterIssueType.DUPLICATE_DBT_CUSTOM_EXTENSION, "avg")]
    owner = next(m for m in result.output.metrics if m.name == "avg")
    assert _user_metadata(owner.config.meta) == USER_META


@pytest.mark.parametrize("prefix", [
    {"materialized": "table"},
    {"meta": {}},
    {"team": "unwrapped-legacy-metadata"},
])
def test_non_meta_entries_do_not_shadow_ratio_user_metadata(prefix):
    doc = _document()
    doc.metrics[0].custom_extensions.insert(0, OssieCustomExtension(
        vendor_name=OssieVendor.DBT.value, data=json.dumps(prefix)))
    forward = OssieToMSIConverter().convert(doc)
    assert forward.issues == []
    owner = next(m for m in forward.output.metrics if m.name == "avg")
    assert _user_metadata(owner.config.meta) == USER_META
    assert set(unchanged_ratio_helpers(forward.output.metrics)) == {"avg"}
    reverse = MSIToOssieConverter().convert(forward.output)
    assert reverse.issues == []
    assert [m.name for m in reverse.output.metrics] == ["avg"]
    assert _user_metadata(json.loads(reverse.output.metrics[0].custom_extensions[0].data)["meta"]) == USER_META


@pytest.mark.parametrize("invalid_meta", [None, [], "invalid", 17])
def test_invalid_wrapped_meta_keeps_warning_and_valid_ratio_provenance(invalid_meta):
    doc = _document()
    doc.metrics[0].custom_extensions.insert(0, OssieCustomExtension(
        vendor_name=OssieVendor.DBT.value, data=json.dumps({"meta": invalid_meta})))
    forward = OssieToMSIConverter().convert(doc)
    assert [(i.issue_type, i.element_name) for i in forward.issues] == [
        (ConverterIssueType.INVALID_DBT_CUSTOM_EXTENSION, "avg")]
    owner = next(m for m in forward.output.metrics if m.name == "avg")
    assert _user_metadata(owner.config.meta) == USER_META
    assert all(m.type_params.is_private is False for m in forward.output.metrics)
    assert set(unchanged_ratio_helpers(forward.output.metrics)) == {"avg"}


@pytest.mark.parametrize("datatype", [OssieDataType.DECIMAL, OssieDataType.DATE_TIME_TZ])
def test_restored_datatype_is_applied_before_signing_ratio_family(datatype):
    meta = deepcopy(USER_META)
    meta["ossie"]["datatype"] = "Integer"
    doc = _document(meta=meta)
    doc = doc.model_copy(update={"metrics": [doc.metrics[0].model_copy(update={"datatype": datatype})]})
    expected = deepcopy(meta)
    expected["ossie"]["datatype"] = datatype.value
    for _ in range(3):
        before = doc.model_dump_json()
        forward = OssieToMSIConverter().convert(doc)
        assert doc.model_dump_json() == before
        assert forward.issues == []
        manifest = PydanticSemanticManifest.parse_raw(forward.output.json(by_alias=True, exclude_none=True))
        owner = next(m for m in manifest.metrics if m.name == "avg")
        assert _user_metadata(owner.config.meta) == expected
        assert set(unchanged_ratio_helpers(manifest.metrics)) == {"avg"}
        reverse = MSIToOssieConverter().convert(manifest)
        assert reverse.issues == []
        assert [m.name for m in reverse.output.metrics] == ["avg"]
        assert reverse.output.metrics[0].datatype == datatype
        assert _user_metadata(json.loads(reverse.output.metrics[0].custom_extensions[0].data)["meta"]) == expected
        doc = OssieDocument.model_validate_json(reverse.output.model_dump_json())
