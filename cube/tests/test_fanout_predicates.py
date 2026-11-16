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

"""Fan-out attribution for a column an aggregate reads only in a CASE/IF condition.

`SUM(CASE WHEN stores.region = 'EU' THEN orders.amount END)` sums `orders` rows. With
`orders -> stores` many-to-one each order joins exactly one store, so the store column
only selects which orders count and multiplies none of them. The model below also has
`shipments -> stores` and `shipment_lines -> shipments`, so `stores` and `shipments`
are both on the one side of some relationship, as a dimension shared by several facts
usually is.
"""

from _util import expr_of, model_of

from ossie_cube import IssueType, convert_cube_to_ossie

_MODEL = (
    "cubes:\n"
    "  - name: orders\n"
    "    sql_table: public.orders\n"
    "    joins:\n"
    "      - name: stores\n"
    "        sql: \"{CUBE}.store_id = {stores}.id\"\n"
    "        relationship: many_to_one\n"
    "    dimensions:\n"
    "      - name: id\n"
    "        sql: id\n"
    "        type: number\n"
    "        primary_key: true\n"
    "      - name: store_id\n"
    "        sql: store_id\n"
    "        type: number\n"
    "    measures:\n"
    "{measures}"
    "  - name: stores\n"
    "    sql_table: public.stores\n"
    "    dimensions:\n"
    "      - name: id\n"
    "        sql: id\n"
    "        type: number\n"
    "        primary_key: true\n"
    "      - name: region\n"
    "        sql: region\n"
    "        type: string\n"
    "  - name: shipments\n"
    "    sql_table: public.shipments\n"
    "    joins:\n"
    "      - name: stores\n"
    "        sql: \"{CUBE}.store_id = {stores}.id\"\n"
    "        relationship: many_to_one\n"
    "    dimensions:\n"
    "      - name: id\n"
    "        sql: id\n"
    "        type: number\n"
    "        primary_key: true\n"
    "      - name: store_id\n"
    "        sql: store_id\n"
    "        type: number\n"
    "      - name: status\n"
    "        sql: status\n"
    "        type: string\n"
    "  - name: shipment_lines\n"
    "    sql_table: public.shipment_lines\n"
    "    joins:\n"
    "      - name: shipments\n"
    "        sql: \"{CUBE}.shipment_id = {shipments}.id\"\n"
    "        relationship: many_to_one\n"
    "    dimensions:\n"
    "      - name: id\n"
    "        sql: id\n"
    "        type: number\n"
    "        primary_key: true\n"
)


def _fanout_issues(measures):
    ossie, issues = convert_cube_to_ossie(
        {"model/cubes/m.yml": _MODEL.replace("{measures}", measures)})
    return model_of(ossie), {
        issue.element_name: issue.detail
        for issue in issues.of_type(IssueType.FANOUT_UNSAFE_METRIC)}


def test_a_filter_on_a_many_to_one_target_leaves_a_sum_fanout_safe():
    model, flagged = _fanout_issues(
        "      - name: eu_revenue\n"
        "        sql: \"{CUBE}.amount\"\n"
        "        type: sum\n"
        "        filters:\n"
        "          - sql: \"{stores}.region = 'EU'\"\n")
    metric = next(m for m in model["metrics"] if m["name"] == "eu_revenue")
    assert expr_of(metric) == (
        "SUM(CASE WHEN (stores.region = 'EU') THEN orders.amount END)")
    assert "orders.eu_revenue" not in flagged


def test_a_case_condition_on_a_many_to_one_target_leaves_a_sum_fanout_safe():
    _, flagged = _fanout_issues(
        "      - name: eu_revenue\n"
        "        sql: \"CASE WHEN {stores}.region = 'EU' THEN {CUBE}.amount END\"\n"
        "        type: sum\n")
    assert "orders.eu_revenue" not in flagged


def test_a_count_of_rows_selected_by_a_many_to_one_target_is_fanout_safe():
    _, flagged = _fanout_issues(
        "      - name: eu_orders\n"
        "        sql: \"CASE WHEN {stores}.region = 'EU' THEN 1 END\"\n"
        "        type: sum\n")
    assert "orders.eu_orders" not in flagged


def test_a_value_read_from_the_one_side_is_still_fanout_unsafe():
    """The condition is on `orders`; the summed value is the store column."""
    _, flagged = _fanout_issues(
        "      - name: store_headcount_of_big_orders\n"
        "        sql: \"CASE WHEN {CUBE}.amount > 100 THEN {stores}.headcount END\"\n"
        "        type: sum\n")
    assert "orders.store_headcount_of_big_orders" in flagged


def test_a_condition_reached_through_a_shared_dimension_is_still_fanout_unsafe():
    """`orders -> stores <- shipments`: joining shipments multiplies the orders summed.

    `orders` does not reach `shipments` through many-to-one joins, so the condition is
    not excused and the check stays as conservative as before.
    """
    _, flagged = _fanout_issues(
        "      - name: delayed_revenue\n"
        "        sql: \"{CUBE}.amount\"\n"
        "        type: sum\n"
        "        filters:\n"
        "          - sql: \"{shipments}.status = 'DELAYED'\"\n")
    assert "orders.delayed_revenue" in flagged


def test_the_reported_cause_is_the_join_from_the_measures_own_cube():
    """`stores` is the one side of both `orders_to_stores` and `shipments_to_stores`;
    a measure on `orders` is multiplied by the first, so that is the one named."""
    _, flagged = _fanout_issues(
        "      - name: store_headcount\n"
        "        sql: \"{stores}.headcount\"\n"
        "        type: sum\n")
    assert "relationship 'orders_to_stores'" in flagged["orders.store_headcount"]
