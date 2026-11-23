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

import copy

import pytest
import yaml
from conftest import assert_golden, fixture, warnings_of

from ossie_databricks import ConversionError, deploy_ossie_model_to_databricks
from ossie_databricks.result import WarningCode


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


def test_target_cannot_equal_source():
    # The orders dataset reads main.raw.orders; deploying into main.raw would make the
    # metric view read its own source table.
    with pytest.raises(ConversionError, match="read by a dataset source"):
        deploy_ossie_model_to_databricks(
            fixture("sources.ossie.yaml"), catalog="main", schema="raw"
        )


def test_query_source_cannot_read_the_target():
    # A query source that reads the table the dataset deploys onto is also a collision.
    model = {
        "version": "0.3.0.dev0",
        "name": "q",
        "datasets": [
            {
                "name": "orders",
                "source": "SELECT * FROM main.sales.orders WHERE o_orderstatus = 'F'",
                "fields": [{"name": "order_id", "expression": {"sql": "o_orderkey"}}],
            }
        ],
    }
    with pytest.raises(ConversionError, match="read by a dataset source"):
        deploy_ossie_model_to_databricks(model, catalog="main", schema="sales")


def test_multi_table_query_source_is_not_a_collision():
    # A query source naming several dotted tables must not crash or read as one table.
    model = {
        "version": "0.3.0.dev0",
        "name": "joins",
        "datasets": [
            {
                "name": "joined",
                "source": "SELECT o.o_custkey FROM main.raw.orders o "
                "JOIN main.raw.customers c ON o.o_custkey = c.c_custkey",
                "fields": [{"name": "customer_id", "expression": {"sql": "o_custkey"}}],
            }
        ],
    }
    result = _deploy(model)
    assert "metric_views/joined.yaml" in result.value


def test_model_metrics_and_relationships_report_individual_omissions():
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    original = copy.deepcopy(model)
    result = _deploy(model)
    omitted = [w for w in result.warnings if w.code == WarningCode.NOT_DEPLOYED]
    assert [w.path for w in omitted] == [
        "metrics[revenue_per_customer]",
        "metrics[average_amount]",
        "relationships[orders_to_customers]",
    ]
    assert all("retained in the Ossie model" in w.message for w in omitted)
    rewritten = yaml.safe_load(result.value["retail.ossie.yaml"])
    assert rewritten["metrics"] == original["metrics"]
    assert rewritten["relationships"] == original["relationships"]
    view = yaml.safe_load(result.value["metric_views/orders.yaml"])
    assert "joins" not in view
    assert not {"revenue_per_customer", "average_amount"} & {m["name"] for m in view["measures"]}
    assert model == original


@pytest.mark.parametrize(
    "missing,dialect",
    [
        (missing, dialect)
        for missing in ("source_column", "unexported_field")
        for dialect in (None, "DATABRICKS", "ANSI_SQL", "OSSIE_SQL_2026")
    ]
    + [("unexported_metric", dialect) for dialect in (None, "DATABRICKS", "OSSIE_SQL_2026")],
)
def test_model_metric_cannot_read_an_unexposed_view_member(dialect, missing):
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    dataset = model["datasets"][0]
    if missing == "source_column":
        expression = "SUM(orders.o_totalprice)"
        unavailable = "o_totalprice"
    elif missing == "unexported_field":
        dataset["fields"].append(
            {
                "name": "hidden_value",
                "expression": {"dialects": [{"dialect": "DAX", "expression": "1"}]},
            }
        )
        expression = "SUM(orders.hidden_value)"
        unavailable = "hidden_value"
    else:
        dataset["metrics"].append(
            {
                "name": "hidden_measure",
                "expression": {"dialects": [{"dialect": "DAX", "expression": "1"}]},
            }
        )
        expression = (
            "MEASURE(orders.hidden_measure)"
            if dialect == "DATABRICKS"
            else "AGG(orders.hidden_measure)"
        )
        unavailable = "hidden_measure"
    model["metrics"] = [
        {
            "name": "unavailable",
            "expression": (
                {"sql": expression}
                if dialect is None
                else {"dialects": [{"dialect": dialect, "expression": expression}]}
            ),
        }
    ]
    with pytest.raises(ConversionError, match="cannot be evaluated over the metric views") as error:
        _deploy(model)
    assert "unavailable" in str(error.value)
    assert unavailable in str(error.value)


def test_model_metric_keeps_qualified_case_insensitive_and_measure_references():
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    model["metrics"][0]["expression"]["sql"] = (
        "AGG(ORDERS.REVENUE) / COUNT(DISTINCT CUSTOMERS.CUSTOMER_ID)"
    )
    model["metrics"][1]["expression"]["sql"] = "AVG(orders.amount)"
    result = _deploy(model)
    assert yaml.safe_load(result.value["retail.ossie.yaml"])["metrics"] == model["metrics"]


def test_non_sql_model_metric_is_retained_and_reported():
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    model["metrics"] = [
        {
            "name": "dax",
            "expression": {
                "dialects": [{"dialect": "DAX", "expression": "COUNTROWS(Orders)"}],
            },
        }
    ]
    result = _deploy(model)
    assert any(
        w.code == WarningCode.NOT_DEPLOYED and w.path == "metrics[dax]" for w in result.warnings
    )
    assert yaml.safe_load(result.value["retail.ossie.yaml"])["metrics"] == model["metrics"]


@pytest.mark.parametrize(
    "expression",
    [
        "MEASURE(orders.revenue) / COUNT(DISTINCT customers.customer_id)",
        "MEASURE(ORDERS.REVENUE) + MEASURE(orders.big_orders)",
        "MEASURE(orders.revenue)",
        "MEASURE(orders.`revenue`) + COUNT_IF(orders.status = 'X')",
        "MEASURE(orders.revenue) OVER (PARTITION BY orders.status)",
        "MEASURE(orders.revenue) FILTER (WHERE orders.status = 'X')",
        "my_udfs.MEASURE(orders.amount)",
        "catalog.my_udfs.MEASURE(orders.amount)",
    ],
)
def test_native_model_metric_can_evaluate_generated_measures(expression):
    model = yaml.safe_load(fixture("databricks_first.ossie.yaml"))
    model["metrics"][0]["expression"]["dialects"][0]["expression"] = expression
    result = _deploy(model)
    rewritten = yaml.safe_load(result.value["databricks_first.ossie.yaml"])
    assert rewritten["metrics"] == model["metrics"]
    assert any(
        w.code == WarningCode.NOT_DEPLOYED and w.path == "metrics[revenue_per_customer]"
        for w in result.warnings
    )


def test_model_metric_reference_check_honors_portable_expression_precedence():
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    model["metrics"][1]["expression"]["dialects"] = [
        {"dialect": "OSSIE_SQL_2026", "expression": "AVG(orders.missing_source_column)"}
    ]
    result = _deploy(model)
    assert yaml.safe_load(result.value["retail.ossie.yaml"])["metrics"] == model["metrics"]


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(orders.revenue)",
        "MEASURE(orders.revenue) + SUM(orders.revenue)",
        "MEASURE(orders.amount)",
        "MEASURE(orders.revenue, orders.discount)",
        "MEASURE()",
        "my_udfs.MEASURE(orders.revenue)",
        "catalog.my_udfs.MEASURE(orders.absent)",
        "MEASURE(orders.revenue) OVER (PARTITION BY orders.absent) + orders.also_absent",
        "MEASURE(orders.revenue) FILTER (WHERE orders.absent > 0) + SUM(orders.revenue)",
    ],
)
def test_native_measure_calls_do_not_make_measures_readable_as_columns(expression):
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    model["metrics"] = [
        {
            "name": "native",
            "expression": {"dialects": [{"dialect": "DATABRICKS", "expression": expression}]},
        }
    ]
    with pytest.raises(ConversionError, match="cannot be evaluated over the metric views"):
        _deploy(model)


def test_native_measure_call_does_not_hide_other_missing_columns():
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    model["metrics"] = [
        {
            "name": "native",
            "expression": {
                "dialects": [
                    {
                        "dialect": "DATABRICKS",
                        "expression": "MEASURE(orders.revenue) + orders.absent",
                    }
                ]
            },
        }
    ]
    with pytest.raises(ConversionError, match="absent"):
        _deploy(model)
