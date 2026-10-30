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

"""Dataset-scoped metrics keep their owner and modeled-field semantics."""

import pytest
from _util import parse, parse_files

from ossie_omni import ConversionError, convert_omni_to_ossie, convert_ossie_to_omni
from ossie_omni._common import dump_yaml


def metric(name, expression, **metadata):
    return {"name": name, "expression": {"dialects": [
        {"dialect": "ANSI_SQL", "expression": expression}]}, **metadata}


def dataset(name, metrics):
    return {"name": name, "source": "sales." + name, "fields": [
        {"name": "net_amount", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "price - discount"}]}},
        {"name": "order_id", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "id"}]}}], "metrics": metrics}


def export(*datasets, metrics=None, **options):
    return convert_ossie_to_omni(dump_yaml({"version": "0.2.0.dev0", "name": "sales",
        "datasets": list(datasets), "metrics": metrics or []}), **options)


@pytest.mark.parametrize("expression,expected", [
    ("SUM(net_amount)", {"sql": "${net_amount}", "aggregate_type": "sum"}),
    ("COUNT(DISTINCT order_id)", {"sql": "${order_id}", "aggregate_type": "count_distinct"}),
    ("COUNT(*)", {"aggregate_type": "count"}),
    ("SUM(net_amount) / COUNT(*)", {"sql": "SUM(${net_amount}) / COUNT(*)"}),
])
def test_dataset_metric_uses_owner_instead_of_topic_base(expression, expected):
    files = export(dataset("orders", []), dataset("refunds", [metric("total", expression)]),
                   base_view="orders")
    assert parse(files["views/refunds.view.yaml"])["measures"]["total"] == expected
    assert "measures" not in parse(files["views/orders.view.yaml"])


def test_same_metric_name_on_different_datasets_and_root_metric_coexist():
    files = export(dataset("orders", [metric("total", "SUM(net_amount)")]),
                   dataset("refunds", [metric("total", "SUM(net_amount)")]),
                   metrics=[metric("balance", "SUM(orders.net_amount) - SUM(refunds.net_amount)")],
                   base_view="orders")
    orders = parse(files["views/orders.view.yaml"])["measures"]
    refunds = parse(files["views/refunds.view.yaml"])["measures"]
    assert orders["total"] == refunds["total"] == {"sql": "${net_amount}", "aggregate_type": "sum"}
    assert orders["balance"]["sql"] == "SUM(${orders.net_amount}) - SUM(${refunds.net_amount})"


def test_dataset_metric_preserves_metadata():
    files = export(dataset("orders", [metric("total", "SUM(net_amount)", description="Net sales",
        ai_context={"synonyms": ["revenue"], "instructions": "After discounts"})]))
    measure = parse(files["views/orders.view.yaml"])["measures"]["total"]
    assert measure == {"sql": "${net_amount}", "aggregate_type": "sum", "description": "Net sales",
                       "synonyms": ["revenue"], "ai_context": "After discounts"}


@pytest.mark.parametrize("expression,expected", [
    ("SUM(CASE WHEN 'net_amount' = 'net_amount' THEN net_amount ELSE 0 END)",
     "CASE WHEN 'net_amount' = 'net_amount' THEN ${net_amount} ELSE 0 END"),
    ("SUM(net_amount /* net_amount */)", "${net_amount} /* net_amount */"),
])
def test_local_field_rewriting_leaves_sql_literals_and_comments_alone(expression, expected):
    files = export(dataset("orders", [metric("total", expression)]))
    assert parse(files["views/orders.view.yaml"])["measures"]["total"]["sql"] == expected


def test_declared_field_name_wins_over_underlying_column_alias():
    ds = dataset("orders", [metric("total", "SUM(net_amount)")])
    ds["fields"].append({"name": "raw_alias", "expression": {"dialects": [
        {"dialect": "ANSI_SQL", "expression": "net_amount"}]}})
    files = export(ds)
    assert parse(files["views/orders.view.yaml"])["measures"]["total"]["sql"] == "${net_amount}"


def test_quoted_declared_field_name_is_sanitized_in_metric_reference():
    ds = dataset("orders", [metric("total", 'SUM("Net.Amount")')])
    ds["fields"][0]["name"] = "Net.Amount"
    files = export(ds)
    assert parse(files["views/orders.view.yaml"])["measures"]["total"]["sql"] == "${net_amount}"


@pytest.mark.parametrize("declared_name,reference", [
    ("net_amount", "NET_AMOUNT"),
    ("Net_Amount", "nEt_AmOuNt"),
])
def test_unquoted_declared_field_reference_is_case_insensitive(declared_name, reference):
    ds = dataset("orders", [metric("total", "SUM(" + reference + ")")])
    ds["fields"][0]["name"] = declared_name
    files = export(ds)
    assert parse(files["views/orders.view.yaml"])["measures"]["total"] == \
        {"sql": "${" + declared_name + "}", "aggregate_type": "sum"}


def test_unquoted_discarded_declared_field_cannot_become_a_raw_column():
    ds = dataset("orders", [metric("total", "SUM(NET_AMOUNT)")])
    ds["fields"][0]["expression"]["dialects"][0]["dialect"] = "MDX"
    with pytest.warns(UserWarning, match="dropping field"):
        with pytest.raises(ConversionError, match="NET_AMOUNT.*no usable expression"):
            export(ds)


def test_quoted_field_reference_keeps_case_sensitive_identity():
    ds = dataset("orders", [metric("total", 'SUM("NET_AMOUNT")')])
    files = export(ds)
    assert parse(files["views/orders.view.yaml"])["measures"]["total"]["sql"] == '"NET_AMOUNT"'


def test_function_name_and_scientific_notation_are_not_field_references():
    ds = dataset("orders", [metric("total", "sum /* sum */ (sum * 1e3)")])
    ds["fields"][0]["name"] = "sum"
    ds["fields"][1]["name"] = "e3"
    files = export(ds)
    assert parse(files["views/orders.view.yaml"])["measures"]["total"]["sql"] == \
        "sum /* sum */ (${sum} * 1e3)"


def test_local_metric_name_colliding_with_a_field_is_rejected():
    with pytest.raises(ConversionError, match="collid|unique"):
        export(dataset("orders", [metric("Net Amount", "COUNT(*)")]))


def test_local_metric_name_colliding_with_root_measure_is_rejected():
    with pytest.raises(ConversionError, match="two metrics"):
        export(dataset("orders", [metric("total", "SUM(net_amount)")]),
               metrics=[metric("total", "COUNT(*)")])


@pytest.mark.parametrize("reference", [
    "refunds.net_amount", '"refunds"."net_amount"', 'refunds."net_amount"',
    '"refunds".net_amount', 'refunds /* owner */ . "net_amount"',
    '"refunds" -- owner\n . net_amount',
])
def test_dataset_metric_cannot_reach_into_another_dataset(reference):
    with pytest.raises(ConversionError, match="dataset|unqualified"):
        export(dataset("orders", [metric("total", "SUM(" + reference + ")")]),
               dataset("refunds", []), base_view="orders")


@pytest.mark.parametrize("expression,expected", [
    ("CAST(order_id AS date)", "CAST(${order_id} AS date)"),
    ("order_id::date", "${order_id}::date"),
    ("CASE WHEN order_id IS NULL THEN 0 ELSE order_id END",
     "CASE WHEN ${order_id} IS NULL THEN 0 ELSE ${order_id} END"),
])
def test_sql_types_and_keywords_are_not_rewritten_as_declared_fields(expression, expected):
    ds = dataset("orders", [metric("total", expression)])
    for name in ("date", "CASE", "WHEN", "IS", "NULL", "THEN", "ELSE", "END"):
        ds["fields"].append({"name": name, "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "source_column"}]}})
    files = export(ds)
    assert parse(files["views/orders.view.yaml"])["measures"]["total"]["sql"] == expected


def test_dataset_metric_without_usable_dialect_warns_and_drops():
    ds = dataset("orders", [{"name": "total", "expression": {"dialects": [
        {"dialect": "MDX", "expression": "[Measures].[Total]"}]}}])
    with pytest.warns(UserWarning, match="dropping metric"):
        files = export(ds)
    assert "measures" not in parse(files["views/orders.view.yaml"])


def test_dataset_metric_omni_files_survive_import_export():
    original = export(dataset("orders", [metric("total", "SUM(net_amount)")]),
                      dataset("refunds", [metric("total", "COUNT(*)")]), base_view="orders")
    imported = convert_omni_to_ossie(original)
    assert len(parse(imported)["metrics"]) == 2  # existing model-scoped import representation
    assert parse_files(convert_ossie_to_omni(imported)) == parse_files(original)
