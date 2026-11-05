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

"""Keep conversion lenient while mapping declared field keys faithfully."""

from pathlib import Path

import pytest
import yaml
from _cube_gate import assert_cube_compiles, assert_ossie_is_valid, cube_gate
from _util import by_name, expr_of, parse_files

from ossie_cube import (
    convert_cube_to_ossie,
    convert_cube_view_to_ossie,
    convert_ossie_to_cube,
)
from ossie_cube._common import OSSIE_VERSION
from ossie_cube.converter_issues import IssueType


def expr(sql, dialect="ANSI_SQL"):
    return {"dialects": [{"dialect": dialect, "expression": sql}]}


def model():
    return {
        "version": OSSIE_VERSION,
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": "sales.public.orders",
                "primary_key": ["id"],
                "fields": [
                    {"name": "id", "expression": expr("order_id")},
                    {"name": "account", "expression": expr("customer_id")},
                    {"name": "amount", "expression": expr("amount")},
                ],
            },
            {
                "name": "customers",
                "source": "sales.public.customers",
                "primary_key": ["id"],
                "fields": [
                    {"name": "id", "expression": expr("customer_id")},
                    {"name": "name", "expression": expr("name")},
                ],
            },
        ],
        "relationships": [
            {
                "name": "customer",
                "from": "orders",
                "to": "customers",
                "from_columns": ["account"],
                "to_columns": ["id"],
            }
        ],
        "metrics": [{"name": "revenue", "expression": expr("SUM(orders.amount)")}],
    }


def export(document):
    return convert_ossie_to_cube(yaml.safe_dump(document))


def native(files):
    return {
        c["name"]: c for f in parse_files(files).values() for c in f.get("cubes", [])
    }


def test_export_accepts_unmodified_databricks_converter_fixture():
    path = (
        Path(__file__).parents[2]
        / "databricks/java/src/test/resources/ossie_fixtureA_ossie.yaml"
    )
    files, issues = convert_ossie_to_cube(path.read_text())
    cubes = native(files)
    dims = by_name(cubes["customer"]["dimensions"])
    assert dims["c_custkey"]["primary_key"] is True
    assert dims["c_custkey"]["public"] is False
    assert cubes["orders"]["joins"][0]["sql"] == "o_custkey = {customer}.c_custkey"
    assert issues.of_type(IssueType.APPROXIMATED)
    returned, _ = convert_cube_to_ossie(files)
    assert_ossie_is_valid(returned)


@pytest.mark.parametrize("constraint", ["primary_key", "unique_keys", "secondary"])
def test_unusable_key_drops_whole_constraint_and_keeps_other_output(constraint):
    doc = model()
    ds = doc["datasets"][0]
    ds["fields"][0]["expression"] = expr("[id]", "DAX")
    if constraint != "primary_key":
        ds.pop("primary_key")
        ds["unique_keys"] = [["id", "account"], ["account"]]
        if constraint == "secondary":
            ds["primary_key"] = ["account"]
    else:
        ds["primary_key"] = ["id", "account"]
    files, issues = export(doc)
    orders = native(files)["orders"]
    assert "id" not in by_name(orders["dimensions"])
    assert by_name(orders["measures"])["revenue"]["type"] == "sum"
    if constraint == "primary_key":
        assert not any(d.get("primary_key") for d in orders["dimensions"])
    else:
        assert orders["meta"]["ossie"]["unique_keys"] == [["account"]]
        assert by_name(orders["dimensions"])["account"]["primary_key"] is True
    assert any("key" in i.element_name and "dropped" in i.detail for i in issues)
    assert any(
        i.element_name == "orders.id"
        for i in issues.of_type(IssueType.NO_USABLE_DIALECT)
    )


@pytest.mark.parametrize("side", ["from", "to"])
def test_unusable_join_field_drops_relationship_even_with_stashed_sql(side):
    doc = model()
    ds = doc["datasets"][0 if side == "from" else 1]
    name = "account" if side == "from" else "id"
    by_name(ds["fields"])[name]["expression"] = expr("[id]", "DAX")
    files, issues = export(doc)
    assert not native(files)["orders"].get("joins")
    assert any(
        "relationship" in i.element_name and "dropped" in i.detail for i in issues
    )
    assert by_name(native(files)["orders"]["measures"])["revenue"]["type"] == "sum"


@pytest.mark.parametrize("constraint", ["primary_key", "unique_keys"])
def test_stale_recorded_key_is_dropped_without_losing_valid_keys(constraint):
    cube = {
        "name": "orders",
        "sql_table": "sales.public.orders",
        "dimensions": [
            {"name": "renamed", "type": "number", "sql": "id", "primary_key": True},
            {"name": "tenant", "type": "number", "sql": "tenant"},
        ],
        "meta": {
            "ossie": {"key_fields": True, "unique_keys": [["renamed"], ["tenant"]]}
        },
    }
    cube["meta"]["ossie"][constraint] = (
        ["old", "tenant"]
        if constraint == "primary_key"
        else [["old", "tenant"], ["renamed"]]
    )
    returned, issues = convert_cube_to_ossie(
        {"model.yml": yaml.safe_dump({"cubes": [cube]})}
    )
    back = yaml.safe_load(returned)
    ds = back["datasets"][0]
    assert "old" not in by_name(ds["fields"])
    assert ds["unique_keys"] == (
        [["renamed"], ["tenant"]] if constraint == "primary_key" else [["renamed"]]
    )
    if constraint == "primary_key":
        assert "primary_key" not in ds
    assert any("old" in i.detail and "dropped" in i.detail for i in issues)
    assert_ossie_is_valid(returned)
    next_files, _ = export(back)
    convert_cube_to_ossie(next_files)


def test_projected_computed_key_count_keeps_native_row_count_and_filters():
    text = """
cubes:
  - name: orders
    sql_table: sales.public.orders
    dimensions:
      - {name: key, sql: "LOWER(email)", type: string, primary_key: true}
      - {name: active, sql: active, type: boolean}
    measures:
      - {name: rows, type: count, filters: [{sql: "{CUBE.active} = true"}]}
      - {name: double_rows, type: number, sql: "{rows} * 2"}
views:
  - name: sales
    cubes:
      - {join_path: orders, includes: [rows, double_rows]}
"""
    returned, _, _ = convert_cube_view_to_ossie({"model.yml": text}, "sales")
    back = yaml.safe_load(returned)
    ds = back["datasets"][0]
    key = ds["primary_key"][0]
    assert f"orders.{key}" in expr_of(by_name(back["metrics"])["rows"])
    assert f"orders.{key}" in expr_of(by_name(back["metrics"])["double_rows"])
    files, _ = export(back)
    rows = by_name(native(files)["orders"]["measures"])["rows"]
    assert rows["type"] == "count" and "sql" not in rows
    assert rows["filters"]


@pytest.mark.parametrize("constraint", ["primary_key", "unique_keys"])
@pytest.mark.parametrize("composite", [False, True])
def test_legacy_keys_use_private_dimensions_without_inventing_a_datatype(
    constraint, composite
):
    doc = model()
    ds = doc["datasets"][0]
    ds.pop("primary_key")
    key = ["legacy_id", "tenant_id"] if composite else ["legacy_id"]
    ds[constraint] = key if constraint == "primary_key" else [key]
    files, issues = export(doc)
    dims = by_name(native(files)["orders"]["dimensions"])
    assert all(dims[n]["primary_key"] and dims[n]["public"] is False for n in key)
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    fields = by_name(by_name(back["datasets"])["orders"]["fields"])
    assert all(expr_of(fields[n]) == n and "datatype" not in fields[n] for n in key)
    assert any(
        "legacy source column" in i.detail
        for i in issues.of_type(IssueType.APPROXIMATED)
    )
    next_files, _ = export(back)
    again, _ = convert_cube_to_ossie(next_files)
    assert parse_files(export(yaml.safe_load(again))[0]) == parse_files(next_files)


@pytest.mark.parametrize("constraint", ["primary_key", "unique_keys"])
def test_legacy_fallback_never_replaces_a_declared_but_unusable_field(constraint):
    doc = model()
    ds = doc["datasets"][0]
    ds["fields"][0]["expression"] = expr("[id]", "DAX")
    ds.pop("primary_key")
    ds[constraint] = ["ID"] if constraint == "primary_key" else [["ID"]]
    files, issues = export(doc)
    assert not any(d.get("primary_key") for d in native(files)["orders"]["dimensions"])
    assert not any("legacy source column" in i.detail for i in issues)
    assert any("key dropped" in i.detail for i in issues)


@pytest.mark.parametrize("side", ["from", "to"])
def test_composite_join_is_not_weakened_when_one_endpoint_is_unusable(side):
    import json

    doc = model()
    for ds in doc["datasets"]:
        ds["fields"].append({"name": "tenant", "expression": expr("tenant")})
    rel = doc["relationships"][0]
    rel["from_columns"].append("tenant")
    rel["to_columns"].append("tenant")
    rel["custom_extensions"] = [
        {
            "vendor_name": "CUBE",
            "data": json.dumps(
                {
                    "_v": 1,
                    "sql": "{CUBE}.customer_id = {customers}.customer_id AND 1 = 1",
                }
            ),
        }
    ]
    ds = doc["datasets"][0 if side == "from" else 1]
    by_name(ds["fields"])["tenant"]["expression"] = expr("[tenant]", "DAX")
    files, issues = export(doc)
    assert not native(files)["orders"].get("joins")
    assert any("entire join" in i.detail for i in issues)


def test_stale_primary_key_does_not_prevent_counting_the_native_key():
    cube = {
        "name": "orders",
        "sql_table": "sales.public.orders",
        "dimensions": [
            {"name": "renamed", "type": "number", "sql": "id", "primary_key": True}
        ],
        "measures": [{"name": "rows", "type": "count"}],
        "meta": {"ossie": {"key_fields": True, "primary_key": ["old"]}},
    }
    returned, issues = convert_cube_to_ossie(
        {"model.yml": yaml.safe_dump({"cubes": [cube]})}
    )
    back = yaml.safe_load(returned)
    assert "primary_key" not in back["datasets"][0]
    assert expr_of(back["metrics"][0]) == "COUNT(DISTINCT orders.renamed)"
    assert len(issues.of_type(IssueType.DROPPED_INVALID_REFERENCE)) == 1
    assert_ossie_is_valid(returned)


@cube_gate
@pytest.mark.parametrize("loss", ["primary_key", "join", "secondary"])
def test_lossy_export_compiles_including_restored_views(loss):
    doc = model()
    if loss == "secondary":
        doc["datasets"][0]["unique_keys"] = [["amount"]]
    files, _ = export(doc)
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    field = {"primary_key": "id", "join": "account", "secondary": "amount"}[loss]
    ds = by_name(back["datasets"])["orders"]
    by_name(ds["fields"])[field]["expression"] = expr("[old]", "DAX")
    # Keep an unrelated metric to establish that loss is local.
    back["metrics"].append({"name": "constant", "expression": expr("1")})
    files, issues = export(back)
    assert issues.of_type(IssueType.NO_USABLE_DIALECT)
    assert_cube_compiles(files, f"drop {loss} dependencies from restored views")


@cube_gate
def test_legacy_databricks_document_compiles_without_fixture_repairs():
    path = (
        Path(__file__).parents[2]
        / "databricks/java/src/test/resources/ossie_fixtureA_ossie.yaml"
    )
    files, _ = convert_ossie_to_cube(path.read_text())
    assert_cube_compiles(files, "original Databricks document with physical keys")


def test_projection_can_count_native_keys_despite_stale_recorded_primary_key():
    cube = {
        "name": "orders",
        "sql_table": "sales.public.orders",
        "dimensions": [
            {"name": "renamed", "type": "number", "sql": "id", "primary_key": True}
        ],
        "measures": [{"name": "rows", "type": "count"}],
        "meta": {"ossie": {"key_fields": True, "primary_key": ["old"]}},
    }
    view = {"name": "sales", "cubes": [{"join_path": "orders", "includes": ["rows"]}]}
    returned, _, issues = convert_cube_view_to_ossie(
        {"model.yml": yaml.safe_dump({"cubes": [cube], "views": [view]})}, "sales"
    )
    back = yaml.safe_load(returned)
    assert "primary_key" not in back["datasets"][0]
    assert expr_of(back["metrics"][0]) == "COUNT(DISTINCT orders.id)"
    assert issues.of_type(IssueType.DROPPED_INVALID_REFERENCE)
    assert_ossie_is_valid(returned)
