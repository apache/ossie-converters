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

import pytest
import yaml
from conftest import assert_golden, fixture, warnings_of

from ossie_databricks import ConversionError, deploy_ossie_model_to_databricks


def _deploy(model, catalog="main"):
    return deploy_ossie_model_to_databricks(model, catalog=catalog, schema="sales")


def test_retail():
    result = _deploy(fixture("retail.ossie.yaml"))
    assert_golden("deploy_retail", {"files": result.value, "warnings": warnings_of(result)})


def test_query_source():
    result = _deploy(fixture("sources.ossie.yaml"))
    view = result.value["metric_views/customer_totals.yaml"]
    assert "source: |-\n  SELECT o_custkey, SUM(o_totalprice) AS total\n" in view
    assert "  GROUP BY o_custkey\ndimensions:" in view
    assert warnings_of(result) == [
        "[METRIC_NOT_PORTABLE] at datasets[customer_totals].metrics[total]: the portable "
        "expression reads source columns, which the metric view does not expose; the metric only "
        "evaluates the measure"
    ]


def test_object_without_sql_is_kept():
    model = yaml.safe_load(fixture("sources.ossie.yaml"))
    dax = {"dialects": [{"dialect": "DAX", "expression": "COUNTROWS(orders)"}]}
    model["datasets"][0]["metrics"] = [{"name": "rows", "expression": dax}]
    result = _deploy(model)
    deployed = yaml.safe_load(result.value["sources.ossie.yaml"])
    assert deployed["datasets"][0]["metrics"] == [{"name": "rows", "expression": dax}]
    assert (
        "[NO_SQL_EXPRESSION] at datasets[orders].metrics[rows]: not in the metric view: no SQL "
        "expression" in warnings_of(result)
    )


def test_names_are_quoted():
    model = fixture("sources.ossie.yaml").replace("name: orders", "name: order lines")
    view = _deploy(model, catalog="dev-catalog").value["metric_views/order lines.yaml"]
    assert view.startswith("# Metric view: `dev-catalog`.sales.`order lines`\nversion: '1.1'\n")


def test_dataset_name_must_be_a_file_name():
    model = fixture("sources.ossie.yaml").replace("name: orders", "name: a/b")
    with pytest.raises(ConversionError, match="file name"):
        _deploy(model)


def test_nested_joins_and_model_metrics():
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    customers = model["datasets"][1]
    customers["fields"].append({"name": "region_id", "expression": {"sql": "region_key"}})
    model["datasets"].append(
        {
            "name": "regions",
            "source": "main.raw.regions",
            "unique_keys": [["id"]],
            "fields": [
                {"name": "id", "expression": {"sql": "region_id"}},
                {"name": "region_name", "expression": {"sql": "name"}},
            ],
        }
    )
    model["relationships"] += [
        {
            "name": "customer_region",
            "from": "customers",
            "to": "regions",
            "from_columns": ["region_id"],
            "to_columns": ["id"],
        },
        {
            "name": "region_fanout",
            "from": "regions",
            "to": "customers",
            "from_columns": ["id"],
            "to_columns": ["region_id"],
        },
    ]
    model["metrics"] = [
        {"name": "doubled_revenue", "expression": {"sql": "raw_revenue * 2"}},
        {"name": "raw_revenue", "expression": {"sql": "SUM(orders.o_totalprice)"}},
        {"name": "region_amount", "expression": {"sql": "SUM(orders.amount + regions.id)"}},
    ]
    result = deploy_ossie_model_to_databricks(model, catalog="main", schema="sales", joins=True)
    orders = yaml.safe_load(result.value["metric_views/orders.yaml"])
    customer = orders["joins"][0]
    assert customer["name"] == "customers"
    assert customer["joins"][0]["on"] == "region_key = region_id"
    assert {"name": "region_name", "expr": "customers.regions.name"} in orders["dimensions"]
    assert {"name": "raw_revenue", "expr": "SUM(o_totalprice)"} in orders["measures"]
    assert {"name": "region_amount", "expr": "SUM(amount + customers.regions.region_id)"} in (
        orders["measures"]
    )
    assert any(w.path == "relationships[region_fanout]" for w in result.warnings)
    assert {"name": "doubled_revenue", "expr": "MEASURE(raw_revenue) * 2"} in orders["measures"]
    deployed = yaml.safe_load(result.value[f"{model['name']}.ossie.yaml"])
    assert deployed["metrics"][1]["expression"]["dialects"][0]["expression"] == (
        "MEASURE(orders.raw_revenue)"
    )


def test_removed_model_metric_cascades_to_dependencies():
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    model["metrics"] = [
        {"name": "raw_revenue", "expression": {"sql": "SUM(orders.o_totalprice)"}},
        {"name": "plus_one", "expression": {"sql": "raw_revenue + 1"}},
        {"name": "avg_amount", "expression": {"sql": "AVG(orders.amount)"}},
    ]
    result = _deploy(model)
    deployed = yaml.safe_load(result.value[f"{model['name']}.ossie.yaml"])
    assert [metric["name"] for metric in deployed["metrics"]] == ["avg_amount"]
    assert {w.path for w in result.warnings if w.code == "NOT_DEPLOYED"} == {
        f"relationships[{model['relationships'][0]['name']}]",
        "metrics[raw_revenue]",
        "metrics[plus_one]",
        "metrics[avg_amount]",
    }
