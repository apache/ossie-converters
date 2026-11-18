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

import yaml

from ossie_databricks import (
    convert_metric_view_to_ossie_model,
    deploy_ossie_model_to_databricks,
)
from ossie_databricks.cli import main

_DEFINITION = """
version: '1.1'
source: samples.tpch.orders
filter: source.o_totalprice > 0
parameters:
- name: cutoff
  type: DOUBLE
joins:
- name: customer
  source: samples.tpch.customer
  on: source.o_custkey = customer.c_custkey
  joins:
  - name: nation
    source: samples.tpch.nation
    using: [nation_id]
- name: ranges
  source: main.raw.ranges
  on: source.o_totalprice > ranges.minimum
dimensions:
- {name: order_id, expr: o_orderkey}
- {name: nation_name, expr: customer.nation.n_name}
- {name: range_name, expr: ranges.name}
measures:
- {name: order_count, expr: COUNT(1)}
- {name: revenue, expr: SUM(o_totalprice)}
- {name: max_balance, expr: MAX(customer.balance)}
- {name: mixed, expr: SUM(source.o_totalprice + customer.balance)}
- {name: twice_revenue, expr: MEASURE(revenue) * 2}
"""


def test_import_and_deploy_metric_view():
    result = convert_metric_view_to_ossie_model(_DEFINITION, "orders")
    model = yaml.safe_load(result.value)
    orders, customer, nation, ranges = model["datasets"]
    assert orders["source"] == "samples.tpch.orders"
    assert customer["source"] == "samples.tpch.customer"
    assert model["relationships"][0]["from_columns"] == ["o_custkey"]
    assert model["relationships"][0]["to_columns"] == ["c_custkey"]
    assert nation["fields"][-1]["name"] == "nation_name"
    assert ranges["fields"][-1]["name"] == "range_name"
    assert orders["metrics"][1]["expression"]["sql"] == "SUM(source.o_totalprice)"
    assert customer["metrics"][0]["expression"]["sql"] == "MAX(source.balance)"
    assert "o_totalprice" in {field["name"] for field in orders["fields"]}
    assert "balance" in {field["name"] for field in customer["fields"]}
    assert model["metrics"][0]["expression"]["sql"] == "SUM(orders.o_totalprice + customer.balance)"
    assert {warning.path for warning in result.warnings} == {
        "filter",
        "parameters",
        "joins[ranges]",
        "measures[twice_revenue]",
    }
    deployed = deploy_ossie_model_to_databricks(
        result.value, catalog="main", schema="semantic", joins=True
    )
    root = yaml.safe_load(deployed.value["metric_views/orders.yaml"])
    assert root["filter"] == "source.o_totalprice > 0"
    assert root["parameters"] == [{"name": "cutoff", "type": "DOUBLE"}]
    assert root["joins"][0]["joins"][0]["using"] == ["nation_id"]
    assert root["joins"][1]["on"] == "source.o_totalprice > ranges.minimum"
    assert {"name": "nation_name", "expr": "customer.nation.n_name"} in root["dimensions"]
    assert {"name": "max_balance", "expr": "MAX(customer.balance)"} in root["measures"]
    assert {"name": "twice_revenue", "expr": "MEASURE(revenue) * 2"} in root["measures"]


def test_import_view_cli(tmp_path):
    source = tmp_path / "metric-view.yaml"
    target = tmp_path / "model.yaml"
    source.write_text(_DEFINITION)
    assert main(["import-view", str(source), "--name", "orders", "-o", str(target)]) == 0
    assert yaml.safe_load(target.read_text())["datasets"][0]["name"] == "orders"
