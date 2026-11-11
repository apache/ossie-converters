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

"""Keys and relationships reference fields, including aliases and computed fields."""

import json

import pytest
import yaml
from _cube_gate import assert_cube_compiles, assert_ossie_is_valid, cube_gate
from _util import by_name, expr_of, parse_files, stash_of

from ossie_cube import (
    convert_cube_to_ossie,
    convert_cube_view_to_ossie,
    convert_ossie_to_cube,
)


from ossie_cube._common import OSSIE_VERSION


def expression(sql):
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": sql}]}


def field(name, sql):
    return {"name": name, "expression": expression(sql), "datatype": "String"}


def document():
    return {
        "version": OSSIE_VERSION,
        "name": "shop",
        "datasets": [
            {
                "name": "orders",
                "source": "sales.public.orders",
                "primary_key": ["Order Key"],
                "fields": [
                    field("Order Key", "LOWER(email)"),
                    field("Customer Key", "COALESCE(customer_id, 0)"),
                ],
            },
            {
                "name": "customers",
                "source": "sales.public.customers",
                "primary_key": ["Customer ID"],
                "fields": [field("Customer ID", "id")],
            },
        ],
        "relationships": [
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["Customer Key"],
                "to_columns": ["Customer ID"],
            }
        ],
    }


def export(model):
    return convert_ossie_to_cube(yaml.safe_dump(model))[0]


def cubes(files):
    return {
        c["name"]: c
        for content in parse_files(files).values()
        for c in content.get("cubes", [])
    }


def assert_declared(model):
    datasets = by_name(model["datasets"])
    for ds in datasets.values():
        names = {f["name"].casefold() for f in ds.get("fields", [])}
        for key in [ds.get("primary_key", [])] + ds.get("unique_keys", []):
            assert all(n.casefold() in names for n in key)
    for rel in model.get("relationships", []):
        for side in ("from", "to"):
            names = {
                f["name"].casefold() for f in datasets[rel[side]].get("fields", [])
            }
            assert all(n.casefold() in names for n in rel[side + "_columns"])


def test_export_resolves_computed_and_renamed_fields_in_keys_and_joins():
    files = export(document())
    native = cubes(files)
    dims = by_name(native["orders"]["dimensions"])
    assert dims["order_key"]["primary_key"] is True
    assert dims["order_key"]["sql"] == "LOWER(email)"
    assert (
        native["orders"]["joins"][0]["sql"]
        == "{CUBE.customer_key} = {customers.customer_id}"
    )
    for _ in range(3):
        returned, _ = convert_cube_to_ossie(files)
        assert_ossie_is_valid(returned)
        model = yaml.safe_load(returned)
        assert_declared(model)
        assert model["datasets"][0]["primary_key"] == ["order_key"]
        assert model["relationships"][0]["from_columns"] == ["customer_key"]
        next_files = export(model)
        assert parse_files(next_files) == parse_files(files)
        files = next_files


@pytest.mark.parametrize("key", ["primary_key", "unique_keys"])
def test_key_matching_is_case_insensitive_and_unique_keys_use_field_expressions(key):
    model = document()
    ds = model["datasets"][0]
    ds.pop("primary_key")
    ds[key] = ["ORDER KEY"] if key == "primary_key" else [["ORDER KEY"]]
    files = export(model)
    assert (
        by_name(cubes(files)["orders"]["dimensions"])["order_key"]["primary_key"]
        is True
    )
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert_declared(back)
    assert ("primary_key" in back["datasets"][0]) == (key == "primary_key")
    assert parse_files(export(back)) == parse_files(files)


@pytest.mark.parametrize(
    "where", ["primary_key", "unique_keys", "from_columns", "to_columns"]
)
def test_undeclared_legacy_columns_are_accepted_and_reported(where):
    model = document()
    if where in ("primary_key", "unique_keys"):
        model["datasets"][0][where] = (
            ["missing"] if where == "primary_key" else [["missing"]]
        )
    else:
        rel = model["relationships"][0]
        rel[where] = ["missing"]
        rel["custom_extensions"] = [
            {"vendor_name": "CUBE", "data": json.dumps({"_v": 1, "sql": "x = y"})}
        ]
    files, issues = convert_ossie_to_cube(yaml.safe_dump(model))
    assert any("missing" in i.detail and "legacy source column" in i.detail for i in issues)
    returned, _ = convert_cube_to_ossie(files)
    assert_declared(yaml.safe_load(returned))


def test_legacy_primary_key_keeps_the_model_metric_name_available():
    model = {
        "version": OSSIE_VERSION,
        "name": "shop",
        "datasets": [{
            "name": "orders", "source": "sales.public.orders",
            "primary_key": ["id"], "fields": [field("amount", "amount")],
        }],
        "metrics": [{"name": "id", "expression": expression("SUM(orders.amount)")}],
    }
    files = export(model)
    order = cubes(files)["orders"]
    assert [measure["name"] for measure in order["measures"]] == ["id"]
    key, = [dimension for dimension in order["dimensions"] if dimension.get("primary_key")]
    assert key["name"] != "id"
    assert key["sql"] == "id"
    assert key["public"] is False
    returned, _ = convert_cube_to_ossie(files)
    assert_declared(yaml.safe_load(returned))


@pytest.mark.parametrize("relationship", ["many_to_one", "one_to_many", "one_to_one"])
@pytest.mark.parametrize("raw", [False, True])
def test_native_joins_choose_declared_aliases_and_preserve_orientation(
    relationship, raw
):
    own = "{CUBE}.customer_id" if raw else "{CUBE.customer_key}"
    other = "{customers}.id" if raw else "{customers.customer_key}"
    native = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "sales.public.orders",
                "dimensions": [
                    {
                        "name": "order_key",
                        "sql": "id",
                        "type": "number",
                        "primary_key": True,
                    },
                    {"name": "customer_key", "sql": "customer_id", "type": "number"},
                ],
                "joins": [
                    {
                        "name": "customers",
                        "relationship": relationship,
                        "sql": f"{own} = {other}",
                    }
                ],
            },
            {
                "name": "customers",
                "sql_table": "sales.public.customers",
                "dimensions": [
                    {
                        "name": "customer_key",
                        "sql": "id",
                        "type": "number",
                        "primary_key": True,
                    }
                ],
            },
        ]
    }
    if relationship == "one_to_many":
        native["cubes"][0]["dimensions"][0].pop("primary_key")
        native["cubes"][0]["dimensions"][1]["primary_key"] = True
    files = {"model.yml": yaml.safe_dump(native)}
    for _ in range(3):
        returned, _ = convert_cube_to_ossie(files)
        model = yaml.safe_load(returned)
        assert_declared(model)
        assert_ossie_is_valid(returned)
        rel = model["relationships"][0]
        assert rel["from_columns"] == rel["to_columns"] == ["customer_key"]
        back = export(model)
        assert parse_files(back) == parse_files(files)
        files = back


def test_raw_column_fields_avoid_computed_fields_and_metric_names():
    native = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "sales.public.orders",
                "dimensions": [
                    {
                        "name": "id",
                        "sql": "LOWER(email)",
                        "type": "string",
                        "primary_key": True,
                    }
                ],
                "measures": [{"name": "source__id", "type": "number", "sql": "1"}],
                "joins": [
                    {
                        "name": "customers",
                        "relationship": "many_to_one",
                        "sql": "{CUBE}.id = {customers}.id",
                    }
                ],
            },
            {
                "name": "customers",
                "sql_table": "sales.public.customers",
                "dimensions": [
                    {"name": "id", "sql": "id", "type": "number", "primary_key": True}
                ],
            },
        ]
    }
    files = {"model.yml": yaml.safe_dump(native)}
    returned, _ = convert_cube_to_ossie(files)
    model = yaml.safe_load(returned)
    assert_declared(model)
    ds = by_name(model["datasets"])["orders"]
    assert ds["primary_key"] == ["id"]
    assert model["relationships"][0]["from_columns"] == ["source__id_2"]
    raw = by_name(ds["fields"])["source__id_2"]
    assert expr_of(raw) == "id"
    assert "datatype" not in raw
    assert stash_of(raw)["raw_column"] is True
    assert parse_files(export(model)) == parse_files(files)


def test_geo_coordinate_keys_keep_the_coordinate_field_and_native_key_dimension():
    model = document()
    ds = model["datasets"][0]
    ds["primary_key"] = ["lat"]
    for name, part, sql in [
        ("lat", "latitude", "latitude"),
        ("lon", "longitude", "longitude"),
    ]:
        f = field(name, sql)
        f["datatype"] = "Float"
        f["custom_extensions"] = [
            {
                "vendor_name": "CUBE",
                "data": json.dumps({"_v": 1, "geo": {"of": "location", "part": part}}),
            }
        ]
        ds["fields"].append(f)
    files = export(model)
    for _ in range(3):
        returned, _ = convert_cube_to_ossie(files)
        back = yaml.safe_load(returned)
        assert_declared(back)
        assert back["datasets"][0]["primary_key"] == ["location_latitude"]
        assert_ossie_is_valid(returned)
        next_files = export(back)
        assert parse_files(next_files) == parse_files(files)
        files = next_files


@cube_gate
def test_field_key_and_join_mappings_compile_in_cube():
    files = export(document())
    assert_cube_compiles(files, "declared field keys and joins")
    returned, _ = convert_cube_to_ossie(files)
    assert_cube_compiles(
        export(yaml.safe_load(returned)), "declared fields after import/export"
    )


def test_projection_keeps_published_key_aliases_and_declares_hidden_join_fields():
    files = export(document())
    files["model/views/shop.yml"] = yaml.safe_dump(
        {
            "views": [
                {
                    "name": "shop",
                    "cubes": [
                        {
                            "join_path": "orders",
                            "includes": [
                                {"name": "order_key", "alias": "order_number"}
                            ],
                        },
                        {
                            "join_path": "orders.customers",
                            "includes": [
                                {"name": "customer_id", "alias": "customer_number"}
                            ],
                        },
                    ],
                }
            ]
        }
    )
    returned, source, _ = convert_cube_view_to_ossie(files, "shop")
    assert source == "orders"
    model = yaml.safe_load(returned)
    assert_declared(model)
    assert_ossie_is_valid(returned)
    ds = by_name(model["datasets"])
    assert ds["orders"]["primary_key"] == ["order_number"]
    assert ds["customers"]["primary_key"] == ["customer_number"]
    rel = model["relationships"][0]
    assert rel["from_columns"] == ["__orders_customer_key"]
    assert rel["to_columns"] == ["customer_number"]
    support = by_name(ds["orders"]["fields"])["__orders_customer_key"]
    assert stash_of(support)["public"] is False
    assert expr_of(support) == "COALESCE(customer_id, 0)"


def test_projection_reuses_hidden_native_key_for_a_raw_join_column():
    native = {
        "cubes": [
            {
                "name": "orders", "sql_table": "sales.public.orders",
                "dimensions": [{"name": "order_id", "type": "number", "primary_key": True}],
                "joins": [{"name": "users", "relationship": "many_to_one",
                           "sql": "{CUBE}.user_id = {users}.id"}],
            },
            {
                "name": "users", "sql_table": "sales.public.users",
                "dimensions": [
                    {"name": "uid", "sql": "id", "type": "number", "primary_key": True},
                    {"name": "city", "type": "string"},
                ],
            },
        ],
        "views": [{"name": "shop", "cubes": [
            {"join_path": "orders", "includes": ["order_id"]},
            {"join_path": "orders.users", "includes": ["city"]},
        ]}],
    }
    returned, _, _ = convert_cube_view_to_ossie({"model.yml": yaml.safe_dump(native)}, "shop")
    model = yaml.safe_load(returned)
    users = by_name(model["datasets"])["users"]
    assert model["relationships"][0]["to_columns"] == users["primary_key"] == ["__users_uid"]
    assert set(by_name(users["fields"])) == {"city", "__users_uid"}
    assert expr_of(by_name(users["fields"])["__users_uid"]) == "id"
    assert_ossie_is_valid(returned)


def test_composite_keys_and_relationships_keep_the_declared_field_order():
    model = document()
    for ds in model["datasets"]:
        ds["fields"].append(field("Tenant Key", "tenant_id"))
        ds["primary_key"].append("Tenant Key")
    rel = model["relationships"][0]
    rel["from_columns"].append("Tenant Key")
    rel["to_columns"].append("Tenant Key")
    files = export(model)
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert_declared(back)
    assert_ossie_is_valid(returned)
    assert back["relationships"][0]["from_columns"] == ["customer_key", "tenant_key"]
    assert back["relationships"][0]["to_columns"] == ["customer_id", "tenant_key"]
    assert back["datasets"][0]["primary_key"] == ["order_key", "tenant_key"]
    assert parse_files(export(back)) == parse_files(files)


def test_legacy_physical_key_metadata_is_migrated_to_a_declared_field():
    native = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "sales.public.orders",
                "meta": {"ossie": {"primary_key": ["id"]}},
                "dimensions": [
                    {"name": "id", "sql": "LOWER(email)", "type": "string"},
                    {
                        "name": "id_pk",
                        "sql": "id",
                        "type": "string",
                        "primary_key": True,
                        "public": False,
                        "meta": {"ossie": {"synthetic_key": True}},
                    },
                ],
                "measures": [{"name": "count", "type": "count"}],
            }
        ]
    }
    returned, _ = convert_cube_to_ossie({"model.yml": yaml.safe_dump(native)})
    model = yaml.safe_load(returned)
    assert_declared(model)
    assert_ossie_is_valid(returned)
    assert model["datasets"][0]["primary_key"] == ["id_pk"]
    assert expr_of(model["metrics"][0]) == "COUNT(DISTINCT orders.id_pk)"
    first = export(model)
    second, _ = convert_cube_to_ossie(first)
    assert parse_files(export(yaml.safe_load(second))) == parse_files(first)


def test_editing_a_restored_relationship_uses_the_new_field_binding():
    files = export(document())
    returned, _ = convert_cube_to_ossie(files)
    model = yaml.safe_load(returned)
    model["relationships"][0]["from_columns"] = ["order_key"]
    back = cubes(export(model))
    assert (
        back["orders"]["joins"][0]["sql"]
        == "{CUBE.order_key} = {customers.customer_id}"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"datatype": "Integer"},
        {"description": "Customer account key"},
        {"expression": expression("COALESCE(customer_id, 0)")},
    ],
)
def test_edits_to_column_only_fields_are_carried_by_private_native_dimensions(changes):
    native = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "sales.public.orders",
                "dimensions": [
                    {"name": "id", "sql": "id", "type": "number", "primary_key": True}
                ],
                "joins": [
                    {
                        "name": "customers",
                        "relationship": "many_to_one",
                        "sql": "{CUBE}.customer_id = {customers}.id",
                    }
                ],
            },
            {
                "name": "customers",
                "sql_table": "sales.public.customers",
                "dimensions": [
                    {"name": "id", "sql": "id", "type": "number", "primary_key": True}
                ],
            },
        ]
    }
    returned, _ = convert_cube_to_ossie({"model.yml": yaml.safe_dump(native)})
    model = yaml.safe_load(returned)
    f = by_name(by_name(model["datasets"])["orders"]["fields"])["customer_id"]
    f.update(changes)
    files = export(model)
    dim = by_name(cubes(files)["orders"]["dimensions"])["customer_id"]
    assert dim["public"] is False
    if "expression" in changes:
        assert cubes(files)["orders"]["joins"][0]["sql"].startswith(
            "{CUBE.customer_id}"
        )
    returned, _ = convert_cube_to_ossie(files)
    restored = by_name(
        by_name(yaml.safe_load(returned)["datasets"])["orders"]["fields"]
    )["customer_id"]
    for key, value in changes.items():
        assert restored[key] == value


def test_editing_a_scalar_join_field_expression_rebuilds_saved_raw_column_sql():
    native = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "sales.public.orders",
                "dimensions": [
                    {
                        "name": "key",
                        "sql": "customer_id",
                        "type": "number",
                        "primary_key": True,
                    }
                ],
                "joins": [
                    {
                        "name": "customers",
                        "relationship": "many_to_one",
                        "sql": "{CUBE}.customer_id = {customers}.id",
                    }
                ],
            },
            {
                "name": "customers",
                "sql_table": "sales.public.customers",
                "dimensions": [
                    {"name": "id", "sql": "id", "type": "number", "primary_key": True}
                ],
            },
        ]
    }
    returned, _ = convert_cube_to_ossie({"model.yml": yaml.safe_dump(native)})
    model = yaml.safe_load(returned)
    by_name(model["datasets"][0]["fields"])["key"]["expression"] = expression(
        "other_customer_id"
    )
    assert (
        cubes(export(model))["orders"]["joins"][0]["sql"]
        == "{CUBE.key} = {customers}.id"
    )


@pytest.mark.parametrize("case", [False, True])
def test_member_key_chains_preserve_escaped_literals(case):
    key = {"name": "key", "type": "string", "primary_key": True}
    if case:
        key["case"] = {
            "when": [{"sql": "{CUBE.middle} IS NOT NULL", "label": r"\{outer\}"}],
            "else": {"label": {"sql": "{CUBE.middle}"}},
        }
    else:
        key["sql"] = r"CONCAT({CUBE.middle}, '\{outer\}')"
    native = {
        "cubes": [
            {
                "name": "orders",
                "sql_table": "sales.public.orders",
                "dimensions": [
                    {
                        "name": "base",
                        "type": "string",
                        "sql": r"CONCAT(id, '\{inner\}')",
                    },
                    {"name": "middle", "type": "string", "sql": "{CUBE.base}"},
                    key,
                ],
            }
        ]
    }
    returned, _ = convert_cube_to_ossie({"model.yml": yaml.safe_dump(native)})
    model = yaml.safe_load(returned)
    sql = expr_of(by_name(model["datasets"][0]["fields"])["key"])
    assert "{inner}" in sql and "{outer}" in sql
    assert "CUBE" not in sql and "\\{" not in sql
    for _ in range(2):
        returned, _ = convert_cube_to_ossie(export(model))
        model = yaml.safe_load(returned)
        assert expr_of(by_name(model["datasets"][0]["fields"])["key"]) == sql


@pytest.mark.parametrize(
    "metadata", [{"format": "currency"}, {"meta": {"owner": "finance"}}]
)
def test_column_only_field_cube_metadata_edits_materialize(metadata):
    model = document()
    raw = {"name": "raw", "expression": expression("raw_id")}
    raw["custom_extensions"] = [
        {
            "vendor_name": "CUBE",
            "data": json.dumps({"_v": 1, "raw_column": True, **metadata}),
        }
    ]
    model["datasets"][0]["fields"].append(raw)
    files = export(model)
    dim = by_name(cubes(files)["orders"]["dimensions"])["raw"]
    assert dim["public"] is False
    for key, value in metadata.items():
        if isinstance(value, dict):
            assert all(dim[key][k] == v for k, v in value.items())
        else:
            assert dim[key] == value
    returned, _ = convert_cube_to_ossie(files)
    restored = by_name(yaml.safe_load(returned)["datasets"][0]["fields"])["raw"]
    for key, value in metadata.items():
        assert stash_of(restored)[key] == value


@pytest.mark.parametrize("primary", [True, False])
def test_secondary_unique_key_with_no_usable_expression_is_dropped_and_reported(primary):
    model = document()
    ds = model["datasets"][0]
    if not primary:
        ds.pop("primary_key")
    ds["unique_keys"] = [["Order Key"], ["Customer Key"]]
    ds["fields"][1]["expression"] = {
        "dialects": [{"dialect": "DAX", "expression": "[Customer Key]"}]
    }
    files, issues = convert_ossie_to_cube(yaml.safe_dump(model))
    assert cubes(files)["orders"]["meta"]["ossie"]["unique_keys"] == [["order_key"]]
    assert any("unique_keys" in i.element_name and "Customer Key" in i.detail for i in issues)
    assert not cubes(files)["orders"].get("joins")


@pytest.mark.parametrize("raw_name", ["source__id", "RAW ID"])
def test_secondary_unique_key_preserves_column_only_field_binding(raw_name):
    model = document()
    ds = model["datasets"][0]
    raw = {
        "name": raw_name,
        "expression": expression("id"),
        "custom_extensions": [
            {"vendor_name": "CUBE", "data": json.dumps({"_v": 1, "raw_column": True})}
        ],
    }
    ds["fields"].append(raw)
    ds["unique_keys"] = [["Order Key"], [raw_name]]
    files = export(model)
    native = cubes(files)["orders"]
    assert native["meta"]["ossie"]["key_fields"] is True
    name = native["meta"]["ossie"]["unique_keys"][1][0]
    assert by_name(native["dimensions"])[name]["public"] is False
    for _ in range(3):
        returned, _ = convert_cube_to_ossie(files)
        back = yaml.safe_load(returned)
        ds = back["datasets"][0]
        assert_declared(back)
        assert expr_of(by_name(ds["fields"])[ds["unique_keys"][1][0]]) == "id"
        next_files = export(back)
        assert parse_files(next_files) == parse_files(files)
        files = next_files


def projected_count_files(model, joined=False):
    if not joined:
        model.pop("relationships", None)
        model["datasets"] = model["datasets"][:1]
    files = export(model)
    native_cubes = cubes(files)
    native = native_cubes["orders"]
    native["measures"] = [{"name": "count", "type": "count"}]
    published = [{"join_path": "orders", "includes": ["count"]}]
    if joined:
        published.append({"join_path": "orders.customers", "includes": ["customer_id"]})
    # Use only the cube, without the automatically generated view/model carrier.
    return {
        "model.yml": yaml.safe_dump(
            {
                "cubes": list(native_cubes.values()),
                "views": [{"name": "published", "cubes": published}],
            }
        )
    }


@pytest.mark.parametrize("promoted", [False, True])
@pytest.mark.parametrize("joined", [False, True])
def test_projection_preserves_hidden_recorded_unique_key_dependencies(promoted, joined):
    model = document()
    ds = model["datasets"][0]
    if promoted:
        ds.pop("primary_key")
    ds["unique_keys"] = [["Order Key"], ["Customer Key"]]
    files = projected_count_files(model, joined)
    returned, _, _ = convert_cube_view_to_ossie(files, "published")
    back = yaml.safe_load(returned)
    assert_declared(back)
    assert_ossie_is_valid(returned)
    ds = by_name(back["datasets"])["orders"]
    assert [
        [expr_of(by_name(ds["fields"])[n]) for n in k] for k in ds["unique_keys"]
    ] == [["LOWER(email)"], ["COALESCE(customer_id, 0)"]]
    assert all(stash_of(f)["public"] is False for f in ds["fields"])
    assert ("primary_key" in ds) != promoted
    export(back)


@pytest.mark.parametrize("part", ["latitude", "longitude"])
def test_projection_resolves_recorded_geo_coordinate_keys(part):
    model = document()
    ds = model["datasets"][0]
    ds["primary_key"] = ["lat" if part == "latitude" else "lon"]
    for name, coordinate in [("lat", "latitude"), ("lon", "longitude")]:
        f = field(name, coordinate)
        f["datatype"] = "Float"
        f["custom_extensions"] = [
            {
                "vendor_name": "CUBE",
                "data": json.dumps(
                    {"_v": 1, "geo": {"of": "location", "part": coordinate}}
                ),
            }
        ]
        ds["fields"].append(f)
    files = projected_count_files(model)
    returned, _, _ = convert_cube_view_to_ossie(files, "published")
    back = yaml.safe_load(returned)
    assert_declared(back)
    assert_ossie_is_valid(returned)
    ds = back["datasets"][0]
    assert expr_of(by_name(ds["fields"])[ds["primary_key"][0]]) == part
    assert expr_of(back["metrics"][0]) == f"COUNT(DISTINCT orders.{ds['primary_key'][0]})"
    # Supporting coordinates are scalar fields in the projected model, so the
    # result can be exported without requiring the unpublished other geo half.
    export(back)


@cube_gate
def test_review_key_dependencies_compile_in_native_cube():
    model = document()
    ds = model["datasets"][0]
    ds["fields"].append(
        {
            "name": "source__id",
            "expression": expression("id"),
            "custom_extensions": [
                {
                    "vendor_name": "CUBE",
                    "data": json.dumps({"_v": 1, "raw_column": True}),
                }
            ],
        }
    )
    ds["unique_keys"] = [["Order Key"], ["source__id"]]
    assert_cube_compiles(export(model), "private secondary unique key")
    returned, _, _ = convert_cube_view_to_ossie(
        projected_count_files(model, True), "published"
    )
    assert_cube_compiles(
        export(yaml.safe_load(returned)), "projected unique-key dependencies"
    )

    ds["primary_key"] = ["lat"]
    for name, part in [("lat", "latitude"), ("lon", "longitude")]:
        ds["fields"].append(
            {
                "name": name,
                "datatype": "Float",
                "expression": expression(part),
                "custom_extensions": [
                    {
                        "vendor_name": "CUBE",
                        "data": json.dumps(
                            {"_v": 1, "geo": {"of": "location", "part": part}}
                        ),
                    }
                ],
            }
        )
    returned, _, _ = convert_cube_view_to_ossie(
        projected_count_files(model), "published"
    )
    assert_cube_compiles(
        export(yaml.safe_load(returned)), "projected geo-coordinate key"
    )
