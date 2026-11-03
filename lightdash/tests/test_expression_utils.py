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

from ossie_lightdash.expression_utils import (
    ParsedAggregation,
    ossie_sql_to_lightdash,
    parse_aggregation,
    referenced_datasets,
)


@pytest.mark.parametrize(
    "expression,expected",
    [
        (
            "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY orders.amount)",
            ParsedAggregation("percentile", "orders.amount", 90),
        ),
        (
            "percentile_cont(0.5) within group (order by COALESCE(orders.amount, 0))",
            ParsedAggregation("percentile", "COALESCE(orders.amount, 0)", 50),
        ),
        (
            "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY orders.a)"
            " + PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY orders.b)",
            None,
        ),
        ("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY orders.a) + 1", None),
        ("SUM(orders.a) + SUM(orders.b)", None),
    ],
)
def test_parse_aggregation_accepts_exactly_one_aggregation(expression, expected):
    assert parse_aggregation(expression) == expected


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("orders.total", "${TABLE}.total"),
        ("customers.name", "${customers.name}"),
        ("'see orders.total'", "'see orders.total'"),
        ("'it''s orders.total'", "'it''s orders.total'"),
        ('"orders.total"', '"orders.total"'),
        ("warehouse.orders.id", "warehouse.orders.id"),
        ("orders.address.city", "${TABLE}.address.city"),
        ("my_orders.total", "my_orders.total"),
        (
            "CASE WHEN orders.note = 'customers.vip' THEN customers.tier ELSE orders.tier END",
            "CASE WHEN ${TABLE}.note = 'customers.vip' THEN ${customers.tier} ELSE ${TABLE}.tier END",
        ),
    ],
)
def test_ossie_sql_to_lightdash_rewrites_only_dataset_references(expression, expected):
    assert ossie_sql_to_lightdash(expression, "orders", {"customers": "customers"}) == expected


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("SUM(orders.amount) / COUNT(customers.id)", {"orders", "customers"}),
        ("'orders.amount'", set()),
        ("warehouse.orders.id", set()),
        ("orders.address.city", {"orders"}),
        ("COUNT(*)", set()),
    ],
)
def test_referenced_datasets_ignores_literals_and_longer_paths(expression, expected):
    assert referenced_datasets(expression, {"orders", "customers"}) == expected


def test_referenced_datasets_with_no_datasets():
    assert referenced_datasets("orders.amount", set()) == set()
