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

"""Dataset-scoped metric export, namespace resolution and round-trip fidelity."""

import pytest
import yaml
from _cube_gate import (
    assert_cube_compiles,
    assert_ossie_is_valid,
    cube_gate,
    validator_gate,
)
from _util import by_name, parse_files, stash_of

from ossie_cube import (
    ConversionError,
    IssueType,
    convert_cube_to_ossie,
    convert_cube_view_to_ossie,
    convert_ossie_to_cube,
)


def expression(sql, dialect="ANSI_SQL"):
    return {"dialects": [{"dialect": dialect, "expression": sql}]}


def dataset(name="orders", metrics=None):
    return {
        "name": name,
        "source": f"sales.public.{name}",
        "primary_key": ["id"],
        "fields": [
            {"name": "id", "expression": expression("id"), "datatype": "Integer"},
            {
                "name": "amount",
                "expression": expression("gross - tax"),
                "datatype": "Decimal",
            },
        ],
        "metrics": metrics or [],
    }


def metric(name="total_amount", sql="SUM(amount)"):
    return {"name": name, "expression": expression(sql)}


def model(*datasets, metrics=None):
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "datasets": list(datasets),
        "metrics": metrics or [],
    }


def export(document):
    return convert_ossie_to_cube(yaml.safe_dump(document), base_cube="orders")


def measures(files, cube="orders"):
    return by_name(
        yaml.safe_load(files[f"model/cubes/{cube}.yml"])["cubes"][0].get("measures")
    )


def test_dataset_metric_uses_its_computed_field_even_with_a_model_metric_of_that_name():
    local = metric()
    local.update(
        datatype="Decimal",
        description="Net revenue",
        ai_context={"synonyms": ["sales"]},
    )
    customers = dataset("customers")
    customers["fields"] = customers["fields"][:1]
    document = model(
        dataset(metrics=[local]),
        customers,
        metrics=[metric("amount", "COUNT(DISTINCT customers.id)")],
    )
    files, issues = export(document)
    measure = measures(files)["total_amount"]
    assert measure["type"] == "sum"
    assert measure["sql"] == "{CUBE.amount}"
    assert measure["description"] == "Net revenue"
    assert not issues.of_type(IssueType.NO_USABLE_DIALECT)
    returned, _ = convert_cube_to_ossie(files)
    back = by_name(yaml.safe_load(returned)["datasets"])["orders"]["metrics"][0]
    for key in ["name", "expression", "datatype", "description", "ai_context"]:
        assert back[key] == local[key]


def test_local_names_are_independent_and_count_star_belongs_to_its_declaring_dataset():
    document = model(
        dataset(metrics=[metric()]),
        dataset("customers", [metric(), metric("row_count", "COUNT(*)")]),
    )
    files, _ = export(document)
    assert measures(files)["total_amount"]["sql"] == "{CUBE.amount}"
    assert measures(files, "customers")["total_amount"]["sql"] == "{CUBE.amount}"
    # COUNT(*) keeps row-count semantics; Cube's bare count deduplicates on PK.
    assert measures(files, "customers")["row_count"]["sql"] == "COUNT(*)"
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert not back.get("metrics")
    datasets = by_name(back["datasets"])
    assert [m["name"] for m in datasets["orders"]["metrics"]] == ["total_amount"]
    assert [m["name"] for m in datasets["customers"]["metrics"]] == [
        "total_amount",
        "row_count",
    ]
    files2, _ = export(back)
    assert parse_files(files2) == parse_files(files)


def test_new_local_metric_is_qualified_when_a_model_metric_already_requests_the_name():
    document = model(
        dataset(metrics=[metric()]),
        metrics=[metric("total_amount", "MAX(orders.amount)")],
    )
    files, _ = export(document)
    assert measures(files)["total_amount"]["type"] == "max"
    assert measures(files)["orders__total_amount"]["type"] == "sum"
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert back["metrics"][0]["name"] == "total_amount"
    assert back["datasets"][0]["metrics"][0]["name"] == "total_amount"
    assert parse_files(export(back)[0]) == parse_files(files)


@pytest.mark.parametrize("with_legacy", [False, True])
def test_primary_sql_survives_for_fields_and_both_metric_scopes(with_legacy):
    def primary(sql, legacy_sql):
        result = {"sql": sql}
        if with_legacy:
            result["dialects"] = expression(legacy_sql, "OSSIE_SQL_2026")["dialects"]
        return result

    ds = dataset(
        metrics=[
            {
                "name": "local_total",
                "expression": primary('SUM("Net Amount")', "MAX(id)"),
            }
        ]
    )
    ds["fields"][1].update(
        name="Net Amount", expression=primary("gross - tax", "gross + tax")
    )
    global_expression = primary("SUM(orders.id)", "MAX(orders.id)")
    files, _ = export(
        model(ds, metrics=[{"name": "global_total", "expression": global_expression}])
    )

    cube = yaml.safe_load(files["model/cubes/orders.yml"])["cubes"][0]
    assert (
        by_name(cube["dimensions"])["net_amount"]["sql"] == "{CUBE}.gross - {CUBE}.tax"
    )
    assert measures(files)["local_total"]["type"] == "sum"
    assert measures(files)["local_total"]["sql"] == "{CUBE.net_amount}"
    assert measures(files)["global_total"]["type"] == "sum"

    returned, _ = convert_cube_to_ossie(files)
    document = yaml.safe_load(returned)
    orders = document["datasets"][0]
    assert by_name(orders["fields"])["net_amount"]["expression"] == primary(
        "gross - tax", "gross + tax"
    )
    assert orders["metrics"][0]["expression"] == primary('SUM("net_amount")', "MAX(id)")
    assert document["metrics"][0]["expression"] == global_expression


def test_invalid_primary_sql_keeps_usable_local_alternative():
    ds = dataset(metrics=[{
        "name": "local_total",
        "expression": {
            "sql": "SUM(missing_column)",
            "dialects": expression("SUM(id)", "ANSI_SQL")["dialects"],
        },
    }])
    files, issues = export(model(ds))
    assert measures(files)["local_total"]["sql"] == "{CUBE}.id"
    assert any("ignored the primary SQL expression" in str(issue) for issue in issues)
    returned, _ = convert_cube_to_ossie(files)
    local = yaml.safe_load(returned)["datasets"][0]["metrics"][0]
    assert local["expression"] == expression("SUM(id)", "ANSI_SQL")


def test_editing_a_restored_local_expression_changes_the_native_operand():
    files, _ = export(model(dataset(metrics=[metric()])))
    returned, _ = convert_cube_to_ossie(files)
    document = yaml.safe_load(returned)
    local = document["datasets"][0]["metrics"][0]
    assert "sql" not in stash_of(local)
    local["expression"] = expression("MAX(id)")
    files, _ = export(document)
    measure = measures(files)["total_amount"]
    assert measure["type"] == "max"
    assert measure["sql"] == "{CUBE}.id"


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "avg"},
        {"sql": "{CUBE}.id"},
        {"filters": [{"sql": "{CUBE}.id > 0"}]},
    ],
)
def test_native_edits_invalidate_the_original_local_expression(changes):
    files, _ = export(model(dataset(metrics=[metric()])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    native["cubes"][0]["measures"][0].update(changes)
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    document = yaml.safe_load(returned)
    assert not document["datasets"][0].get("metrics")
    assert document["metrics"][0]["expression"] != expression("SUM(amount)")
    assert any("origin metadata no longer matches" in issue.detail for issue in issues)
    back, _ = export(document)
    for key, value in changes.items():
        assert measures(back)["total_amount"][key] == value


def test_descriptive_native_edits_keep_local_scope():
    files, _ = export(model(dataset(metrics=[metric()])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    native["cubes"][0]["measures"][0]["description"] = "Updated description"
    files[path] = yaml.safe_dump(native)
    returned, _ = convert_cube_to_ossie(files)
    restored = yaml.safe_load(returned)["datasets"][0]["metrics"][0]
    assert restored["description"] == "Updated description"


@pytest.mark.parametrize("bad_origin", [True, "local", {"name": "local"}])
def test_malformed_origin_metadata_falls_back_to_native_import(bad_origin):
    files, _ = export(model(dataset(metrics=[metric()])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    native["cubes"][0]["measures"][0]["meta"]["ossie"]["dataset_metric"] = bad_origin
    files[path] = yaml.safe_dump(native)
    returned, _ = convert_cube_to_ossie(files)
    assert yaml.safe_load(returned)["metrics"][0]["name"] == "total_amount"


@validator_gate
def test_field_sanitization_cannot_create_duplicate_local_metric_names():
    ds = dataset(
        metrics=[
            metric("gross_amount", 'SUM("Gross Amount")'),
            metric("orders__gross_amount", 'MAX("Gross Amount")'),
        ]
    )
    ds["fields"][1]["name"] = "Gross Amount"
    files, issues = export(model(ds))
    assert any("restored field names would collide" in issue.detail for issue in issues)
    returned, _ = convert_cube_to_ossie(files)
    assert_ossie_is_valid(returned, "sanitized local namespace")
    restored = yaml.safe_load(returned)["datasets"][0]
    names = [m["name"] for m in restored["metrics"]]
    assert len(set(names)) == 2
    assert "gross_amount" not in names
    assert parse_files(export(yaml.safe_load(returned))[0]) == parse_files(files)


def test_explicit_native_overrides_survive_local_metric_import():
    local = metric()
    local["nonportable_extensions"] = [
        {
            "vendor_name": "CUBE",
            "data": {"_v": 1, "type": "number", "sql": "SUM({CUBE.amount})"},
        }
    ]
    files, _ = export(model(dataset(metrics=[local])))
    returned, _ = convert_cube_to_ossie(files)
    restored = yaml.safe_load(returned)["datasets"][0]["metrics"][0]
    assert stash_of(restored)["type"] == "number"
    assert stash_of(restored)["sql"] == "SUM({CUBE.amount})"
    assert parse_files(export(yaml.safe_load(returned))[0]) == parse_files(files)


@pytest.mark.parametrize("source_dialect", [None, "ANSI_SQL"])
@pytest.mark.parametrize(
    "bad_expression",
    [
        {},
        {"dialects": None},
        {"dialects": {}},
        {"dialects": "ANSI_SQL"},
        {"dialects": []},
        {"dialects": [None]},
        {"dialects": ["SUM(amount)"]},
        {"dialects": [{}]},
        {"dialects": [{"dialect": [], "expression": "SUM(amount)"}]},
        {"dialects": [{"dialect": "ANSI_SQL", "expression": None}]},
        {"dialects": [{"dialect": "ANSI_SQL", "expression": ""}]},
        {"dialects": [{"dialect": "unknown", "expression": "SUM(amount)"}]},
    ],
)
def test_malformed_parked_expressions_fall_back_to_native_import(
    bad_expression, source_dialect
):
    files, _ = export(model(dataset(metrics=[metric()])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    origin = native["cubes"][0]["measures"][0]["meta"]["ossie"]["dataset_metric"]
    origin["expression"] = bad_expression
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files, source_dialect=source_dialect)
    document = yaml.safe_load(returned)
    assert not document["datasets"][0].get("metrics")
    assert document["metrics"][0]["expression"] == expression("SUM(orders.amount)")
    assert any("origin metadata no longer matches" in issue.detail for issue in issues)


@pytest.mark.parametrize("whitespace", [" ", "\n\t", "\r\n  "])
def test_leading_whitespace_does_not_shift_local_field_replacements(whitespace):
    ds = dataset(metrics=[metric(sql=whitespace + 'SUM("Net Amount") + MAX(id)  ')])
    ds["fields"][1]["name"] = "Net Amount"
    files, _ = export(model(ds))
    assert measures(files)["total_amount"]["sql"] == (
        "SUM({CUBE.net_amount}) + MAX({CUBE}.id)"
    )
    returned, _ = convert_cube_to_ossie(files)
    assert parse_files(export(yaml.safe_load(returned))[0]) == parse_files(files)


@pytest.mark.parametrize(
    "dialect,quote",
    [("ANSI_SQL", '"'), ("SNOWFLAKE", '"'), ("DATABRICKS", "`"), ("BIGQUERY", "`")],
)
def test_sanitized_intrinsic_names_remain_quoted_fields(dialect, quote):
    ds = dataset(metrics=[metric()])
    ds["fields"][1]["name"] = "Current Date"
    ds["metrics"][0]["expression"] = expression(
        f"MIN({quote}Current Date{quote})", dialect
    )
    files, _ = export(model(ds))
    assert measures(files)["total_amount"]["sql"] == "{CUBE.current_date}"
    for _ in range(2):
        returned, _ = convert_cube_to_ossie(files)
        back = yaml.safe_load(returned)
        assert back["datasets"][0]["metrics"][0]["expression"] == expression(
            f"MIN({quote}current_date{quote})", dialect
        )
        next_files, _ = export(back)
        assert parse_files(next_files) == parse_files(files)
        files = next_files


@pytest.mark.parametrize("member_name", ["lat_key", "LAT_KEY", "lat_key_pk"])
def test_private_geo_key_dimension_does_not_rename_an_allocated_measure(member_name):
    rows = metric("rows", "COUNT(DISTINCT lat_key)")
    rows["nonportable_extensions"] = [
        {"vendor_name": "CUBE", "data": {"_v": 1, "name": member_name}}
    ]
    ds = dataset(metrics=[rows])
    ds["primary_key"] = ["lat_key"]
    for name, part, column in [
        ("lat_key", "latitude", "lat"),
        ("lon", "longitude", "lon"),
    ]:
        ds["fields"].append(
            {
                "name": name,
                "expression": expression(column),
                "datatype": "Float",
                "nonportable_extensions": [
                    {
                        "vendor_name": "CUBE",
                        "data": {"_v": 1, "geo": {"of": "location", "part": part}},
                    }
                ],
            }
        )
    files, _ = export(model(ds))
    cube = yaml.safe_load(files["model/cubes/orders.yml"])["cubes"][0]
    key = next(d for d in cube["dimensions"] if d.get("primary_key"))
    assert key["name"].lower() != member_name.lower()
    assert key["sql"] == "{CUBE}.lat" and key["public"] is False
    assert measures(files)[member_name]["type"] == "count"
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert back["datasets"][0]["primary_key"] == ["location_latitude"]
    assert back["datasets"][0]["metrics"][0]["expression"] == expression(
        "COUNT(DISTINCT location_latitude)"
    )
    assert parse_files(export(back)[0]) == parse_files(files)


def test_legacy_key_column_does_not_shadow_a_local_metric():
    ds = dataset(metrics=[metric("id", "SUM(amount)")])
    ds["fields"] = ds["fields"][1:]
    files, _ = export(model(ds))
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    ds = back["datasets"][0]
    assert ds["primary_key"] == ["source__id"]
    assert ds["metrics"][0]["name"] == "id"
    assert by_name(ds["fields"])["source__id"]["expression"] == expression("id")
    assert parse_files(export(back)[0]) == parse_files(files)


def test_local_metric_keeps_its_column_only_field_without_a_join():
    ds = dataset(metrics=[metric("volume", "SUM(units)")])
    ds["fields"].append(
        {
            "name": "units",
            "expression": expression("quantity"),
            "nonportable_extensions": [
                {"vendor_name": "CUBE", "data": {"_v": 1, "raw_column": True}}
            ],
        }
    )
    files, _ = export(model(ds))
    cube = yaml.safe_load(files["model/cubes/orders.yml"])["cubes"][0]
    assert by_name(cube["dimensions"])["units"]["public"] is False
    assert measures(files)["volume"]["sql"] == "{CUBE.units}"
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert back["datasets"][0]["metrics"][0]["expression"] == expression("SUM(units)")
    assert parse_files(export(back)[0]) == parse_files(files)


@validator_gate
@pytest.mark.parametrize("renamed_coordinates", [False, True])
def test_geo_coordinates_restore_the_fields_emitted_by_import(renamed_coordinates):
    ds = dataset(
        metrics=[metric("location", "AVG(lat_field)"), metric("location_latitude")]
    )
    for part, source, name in [
        ("latitude", "lat", "lat_field"),
        ("longitude", "lon", "lon_field"),
    ]:
        ds["fields"].append(
            {
                "name": name if renamed_coordinates else f"location_{part}",
                "expression": expression(source),
                "datatype": "Float",
                "dimension": {},
                "nonportable_extensions": [
                    {
                        "vendor_name": "CUBE",
                        "data": {"_v": 1, "geo": {"of": "location", "part": part}},
                    }
                ],
            }
        )
    if not renamed_coordinates:
        ds["metrics"] = [metric("location", "AVG(location_latitude)")]
    files, _ = export(model(ds))
    assert measures(files)["orders__location"]["sql"] == "{CUBE}.lat"
    for _ in range(2):
        returned, _ = convert_cube_to_ossie(files)
        assert_ossie_is_valid(returned, "dataset metric over a geo coordinate")
        back = yaml.safe_load(returned)
        locals = by_name(back["datasets"][0]["metrics"])
        assert "location" in locals
        assert locals["location"]["expression"] == expression("AVG(location_latitude)")
        fields = by_name(back["datasets"][0]["fields"])
        assert not fields.keys() & locals.keys()
        next_files, _ = export(back)
        assert parse_files(next_files) == parse_files(files)
        files = next_files


def test_authored_native_name_override_survives_with_view_references():
    local = metric()
    local["nonportable_extensions"] = [
        {"vendor_name": "CUBE", "data": {"_v": 1, "name": "native_total"}}
    ]
    files, _ = export(model(dataset(metrics=[local])))
    assert "native_total" in measures(files)
    files["model/views/curated.yml"] = yaml.safe_dump(
        {
            "views": [
                {
                    "name": "curated",
                    "cubes": [{"join_path": "orders", "includes": ["native_total"]}],
                }
            ]
        }
    )
    for _ in range(2):
        returned, _ = convert_cube_to_ossie(files)
        back = yaml.safe_load(returned)
        restored = back["datasets"][0]["metrics"][0]
        assert restored["name"] == "total_amount"
        assert stash_of(restored)["name"] == "native_total"
        next_files, _ = export(back)
        assert parse_files(next_files) == parse_files(files)
        files = next_files


@pytest.mark.parametrize(
    "dialect,quote",
    [
        ("ANSI_SQL", '"'),
        ("OSSIE_SQL_2026", '"'),
        ("SNOWFLAKE", '"'),
        ("DATABRICKS", "`"),
        ("BIGQUERY", "`"),
    ],
)
def test_quoted_local_fields_resolve_in_the_selected_dialect(dialect, quote):
    ds = dataset(metrics=[metric()])
    ds["fields"][1]["name"] = "Net Amount"
    sql = f"SUM({quote}Net Amount{quote}) + SUM(CASE WHEN id > 0 THEN 1 ELSE 0 END)"
    ds["metrics"][0]["expression"] = expression(sql, dialect)
    files, _ = export(model(ds))
    measure = measures(files)["total_amount"]
    assert "{CUBE.net_amount}" in measure["sql"]
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    normalized_sql = sql.replace(
        f"{quote}Net Amount{quote}", f"{quote}net_amount{quote}"
    )
    assert back["datasets"][0]["metrics"][0]["expression"] == expression(
        normalized_sql, dialect
    )
    assert "net_amount" in by_name(back["datasets"][0]["fields"])
    assert parse_files(export(back)[0]) == parse_files(files)


def test_case_comments_and_literals_are_not_treated_as_local_fields():
    sql = "SUM(CASE WHEN id > 0 /* amount */ THEN amount END) + LENGTH('amount {id}')"
    files, _ = export(model(dataset(metrics=[metric(sql=sql)])))
    measure = measures(files)["total_amount"]
    assert "{CUBE.amount}" in measure["sql"]
    assert "'amount \\{id\\}'" in measure["sql"]
    returned, _ = convert_cube_to_ossie(files)
    assert yaml.safe_load(returned)["datasets"][0]["metrics"][0][
        "expression"
    ] == expression(sql)


def test_an_opaque_alternative_is_preserved_and_diagnosed_after_field_sanitization():
    ds = dataset(metrics=[metric(sql='SUM("Net Amount")')])
    ds["fields"][1]["name"] = "Net Amount"
    opaque = {"dialect": "DAX", "expression": "SUM([Net Amount])"}
    ds["metrics"][0]["expression"]["dialects"].append(opaque)
    files, issues = export(model(ds))
    assert measures(files)["total_amount"]["sql"] == "{CUBE.net_amount}"
    assert any(
        "opaque DAX alternative" in issue.detail
        for issue in issues.of_type(IssueType.APPROXIMATED)
    )
    returned, _ = convert_cube_to_ossie(files)
    restored = yaml.safe_load(returned)["datasets"][0]["metrics"][0]
    assert opaque in restored["expression"]["dialects"]


@pytest.mark.parametrize(
    "sql,match",
    [
        ("SUM(missing)", "not a declared field"),
        ("SUM(orders.amount)", "unqualified declared fields"),
        ("SUM(customers.amount)", "unqualified declared fields"),
        ("SUM(gross)", "not a declared field"),
        ("total_amount * 2", "not a declared field"),
        ("SUM(", "expected a SQL expression"),
        ("SELECT SUM(amount)", "expected a SQL expression"),
        ("SUM(amount); SUM(id)", "expected a SQL expression"),
        ("SUM((SELECT amount))", "expected a SQL expression"),
    ],
)
def test_invalid_local_expression_drops_only_the_affected_metric(sql, match):
    files, issues = export(
        model(
            dataset(metrics=[metric("invalid", sql), metric()]),
            dataset("customers", [metric()]),
        )
    )
    assert set(measures(files)) == set(measures(files, "customers")) == {"total_amount"}
    dropped = issues.of_type(IssueType.DROPPED_NO_CUBE_EQUIVALENT)
    assert len(dropped) == 1
    assert dropped[0].element_name == "dataset 'orders' metric 'invalid'"
    assert match in dropped[0].detail and "metric dropped" in dropped[0].detail


@pytest.mark.parametrize("name", ["amount", "AMOUNT", "total", "TOTAL"])
def test_dataset_fields_and_metrics_share_a_case_insensitive_namespace(name):
    with pytest.raises(ConversionError, match="share one dataset namespace"):
        export(model(dataset(metrics=[metric("total"), metric(name)])))


def test_missing_field_dialect_drops_the_metric_with_a_scoped_diagnostic():
    ds = dataset(metrics=[metric()])
    ds["fields"][1]["expression"] = expression("[amount]", "DAX")
    files, issues = export(model(ds))
    assert not measures(files)
    assert any(
        "dataset 'orders' metric 'total_amount'" == issue.element_name
        and "references" in issue.detail
        for issue in issues.of_type(IssueType.NO_USABLE_DIALECT)
    )


def test_unsupported_metric_dialect_is_reported_instead_of_silently_dropped():
    ds = dataset(metrics=[metric()])
    ds["metrics"][0]["expression"] = expression("[amount]", "DAX")
    files, issues = export(model(ds))
    assert not measures(files)
    assert any(
        issue.element_name == "dataset 'orders' metric 'total_amount'"
        for issue in issues.of_type(IssueType.NO_USABLE_DIALECT)
    )


def test_projection_publishes_a_dataset_measure_with_local_scope():
    files, _ = export(model(dataset(metrics=[metric()])))
    projected, source, _ = convert_cube_view_to_ossie(files, "shop")
    document = yaml.safe_load(projected)
    assert source == "orders"
    restored = document["datasets"][0]["metrics"][0]
    assert restored["name"] == "total_amount"
    assert "source." in restored["expression"]["dialects"][0]["expression"]
    assert not document.get("metrics")


def test_primary_key_count_and_multiple_dialects_survive_repeated_round_trips():
    local = metric("order_count", "COUNT(DISTINCT id)")
    local["expression"]["dialects"].append(
        {"dialect": "SNOWFLAKE", "expression": "COUNT(DISTINCT ID)"}
    )
    local["nonportable_extensions"] = [
        {"vendor_name": "COMMON", "data": {"unit": "orders"}}
    ]
    files, _ = export(model(dataset(metrics=[local])))
    assert measures(files)["order_count"]["type"] == "count"
    assert "sql" not in measures(files)["order_count"]
    for _ in range(3):
        returned, _ = convert_cube_to_ossie(files)
        back = yaml.safe_load(returned)
        restored = back["datasets"][0]["metrics"][0]
        assert restored["expression"] == local["expression"]
        assert local["nonportable_extensions"][0] in restored["nonportable_extensions"]
        next_files, _ = export(back)
        assert parse_files(next_files) == parse_files(files)
        files = next_files


@pytest.mark.parametrize("bad_metrics", [{}, "total_amount", ["total_amount"]])
def test_malformed_dataset_metric_collections_raise_a_conversion_error(bad_metrics):
    ds = dataset()
    ds["metrics"] = bad_metrics
    with pytest.raises(ConversionError, match="must be a (list|mapping)"):
        export(model(ds))


@validator_gate
def test_imported_local_metrics_pass_the_pinned_ossie_validator():
    files, _ = export(
        model(dataset(metrics=[metric()]), dataset("customers", [metric()]))
    )
    returned, _ = convert_cube_to_ossie(files)
    assert_ossie_is_valid(returned, "dataset-scoped metrics")


@cube_gate
def test_local_and_model_metrics_compile_together_in_cube():
    files, _ = export(
        model(
            dataset(metrics=[metric(), metric("row_count", "COUNT(*)")]),
            metrics=[metric("total_amount", "MAX(orders.amount)")],
        )
    )
    assert_cube_compiles(files, "dataset-scoped metrics")


@cube_gate
def test_review_regressions_compile_in_cube_with_preserved_view_references():
    ds = dataset(
        metrics=[metric("id_pk"), metric("first_date", ' \n MIN("Current Date")')]
    )
    ds["fields"][0]["expression"] = expression("LOWER(email)")
    ds["fields"].append(
        {
            "name": "Current Date",
            "expression": expression("created_at"),
            "datatype": "Date",
        }
    )
    ds["metrics"][1]["nonportable_extensions"] = [
        {"vendor_name": "CUBE", "data": {"_v": 1, "name": "native_date"}}
    ]
    files, _ = export(model(ds))
    files["model/views/curated.yml"] = yaml.safe_dump(
        {
            "views": [
                {
                    "name": "curated",
                    "cubes": [{"join_path": "orders", "includes": ["native_date"]}],
                }
            ]
        }
    )
    assert_cube_compiles(files, "review regressions")
    returned, _ = convert_cube_to_ossie(files)
    next_files, _ = export(yaml.safe_load(returned))
    assert parse_files(next_files) == parse_files(files)
    assert_cube_compiles(next_files, "review regressions after import/export")


@pytest.mark.parametrize(
    "dialect,quote", [("ANSI_SQL", '"'), ("DATABRICKS", "`"), ("BIGQUERY", "`")]
)
def test_source_columns_bypass_declared_fields_and_preserve_dialect(dialect, quote):
    local = metric()
    local["expression"] = expression(
        f"SUM(SOURCE.amount) + SUM(amount) + SUM(source.{quote}unit price{quote})",
        dialect,
    )
    files, _ = export(model(dataset(metrics=[local])))
    assert measures(files)["total_amount"]["sql"] == (
        f"SUM({{CUBE}}.amount) + SUM({{CUBE.amount}}) + SUM({{CUBE}}.{quote}unit price{quote})"
    )
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert back["datasets"][0]["metrics"][0]["expression"] == local["expression"]
    assert parse_files(export(back)[0]) == parse_files(files)


def test_source_field_shadows_qualified_source_columns():
    ds = dataset(
        metrics=[metric("bad", "SUM(Source.amount)"), metric("ok", "SUM(source)")]
    )
    ds["fields"].append({"name": "source", "expression": expression("quantity")})
    files, issues = export(model(ds))
    assert set(measures(files)) == {"ok"}
    assert measures(files)["ok"]["sql"] == "{CUBE.source}"
    assert any("field named source shadows" in i.detail for i in issues)


def test_source_column_count_does_not_become_a_bare_cube_count():
    files, _ = export(
        model(dataset(metrics=[metric("raw_count", "COUNT(DISTINCT source.id)")]))
    )
    native = measures(files)["raw_count"]
    assert native["type"] == "count_distinct"
    assert native["sql"] == "{CUBE}.id"


def _curate_total(files):
    files["model/views/shop.yml"] = yaml.safe_dump(
        {
            "views": [
                {
                    "name": "shop",
                    "cubes": [{"join_path": "orders", "includes": ["total_amount"]}],
                }
            ]
        }
    )


def _collision_after_import():
    files, _ = export(model(dataset(metrics=[metric()])))
    _curate_total(files)
    returned, _ = convert_cube_to_ossie(files)
    doc = yaml.safe_load(returned)
    doc["metrics"] = [
        metric("derived", "total_amount * 2"),
        metric("orders__total_amount", "MIN(orders.id)"),
        metric("total_amount", "MAX(orders.amount)"),
    ]
    return export(doc)


def test_adding_a_model_metric_preserves_the_member_already_used_by_a_view():
    files, issues = _collision_after_import()
    assert measures(files)["total_amount"]["type"] == "sum"
    assert measures(files)["orders__total_amount_2"]["type"] == "max"
    assert measures(files)["derived"]["sql"] == "{orders__total_amount_2} * 2"
    assert any("dataset metric keeps its Cube member name" in i.detail for i in issues)
    assert yaml.safe_load(files["model/views/shop.yml"])["views"][0]["cubes"][0][
        "includes"
    ] == ["total_amount"]
    returned, _ = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert by_name(back["metrics"])["total_amount"]["expression"] == expression(
        "MAX(orders.amount)"
    )
    assert by_name(back["metrics"])["derived"]["expression"] == expression(
        "total_amount * 2"
    )
    assert parse_files(export(back)[0]) == parse_files(files)


@pytest.mark.parametrize("edit", ["sql", "rename", "delete", "unrelated", "label"])
def test_field_binding_edits_cannot_restore_an_incorrect_local_expression(edit):
    ds = dataset(metrics=[metric(), metric("minimum", "MIN(id)")])
    ds["fields"][1]["expression"] = expression("amount")
    files, _ = export(model(ds))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    cube = native["cubes"][0]
    amount = by_name(cube["dimensions"])["amount"]
    if edit == "sql":
        amount["sql"] = "amount_usd"
    elif edit == "rename":
        amount["name"] = "old_amount"
    elif edit == "delete":
        cube["dimensions"].remove(amount)
    elif edit == "unrelated":
        cube["dimensions"].append(
            {"name": "unrelated", "sql": "price", "type": "number"}
        )
    else:
        amount["title"] = "Amount in dollars"
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    assert_ossie_is_valid(returned)
    back = yaml.safe_load(returned)
    ds = back["datasets"][0]
    local_names = set(by_name(ds["metrics"]))
    if edit in {"sql", "rename", "delete"}:
        assert local_names == {"minimum"}
        assert not back.get("metrics")
        assert stash_of(ds)["extra_measures"][0]["measure"]["sql"] == "{CUBE}.amount"
        assert any(
            "field bindings changed" in i.detail
            for i in issues.of_type(IssueType.PARKED_IN_META)
        )
    else:
        assert local_names == {"minimum", "total_amount"}
    assert parse_files(export(back)[0]) == parse_files(files)


@cube_gate
def test_review_collision_and_raw_source_compile_with_the_native_compiler():
    files, _ = _collision_after_import()
    assert_cube_compiles(files, "colliding metric with a curated view")
    files, _ = export(
        model(dataset(metrics=[metric(sql="SUM(source.amount) + SUM(amount)")]))
    )
    assert_cube_compiles(files, "raw source column shadowed by a declared field")


def test_edited_native_operand_with_no_declared_field_is_parked():
    files, _ = export(model(dataset(metrics=[metric()])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    native["cubes"][0]["measures"][0]["sql"] = "{CUBE}.raw_quantity"
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    assert_ossie_is_valid(returned)
    back = yaml.safe_load(returned)
    assert not back.get("metrics") and not back["datasets"][0].get("metrics")
    assert (
        stash_of(back["datasets"][0])["extra_measures"][0]["measure"]["sql"]
        == "{CUBE}.raw_quantity"
    )
    assert any("no valid model-level field binding" in i.detail for i in issues)


def test_changed_field_dependency_preserves_native_calculated_dependents():
    files, _ = export(model(dataset(metrics=[metric()])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    cube = native["cubes"][0]
    by_name(cube["dimensions"])["amount"]["sql"] = "gross_usd - tax_usd"
    cube["measures"].append(
        {"name": "derived", "type": "number", "sql": "{total_amount} * 2"}
    )
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert not back.get("metrics")
    parked = stash_of(back["datasets"][0])["extra_measures"]
    assert [m["measure"]["name"] for m in parked] == ["total_amount", "derived"]
    assert len(issues.of_type(IssueType.PARKED_IN_META)) == 2
    assert parse_files(export(back)[0]) == parse_files(files)


def test_bare_count_is_not_restored_after_native_key_changes():
    files, _ = export(model(dataset(metrics=[metric("rows", "COUNT(DISTINCT id)")])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    cube = native["cubes"][0]
    assert cube["measures"][0]["type"] == "count"
    by_name(cube["dimensions"])["id"].pop("primary_key")
    by_name(cube["dimensions"])["amount"]["primary_key"] = True
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert not back["datasets"][0].get("metrics")
    assert any("row-count key changed" in i.detail for i in issues)


def test_field_signature_checks_the_native_member_chain():
    ds = dataset(metrics=[metric()])
    ds["fields"][1]["nonportable_extensions"] = [
        {"vendor_name": "CUBE", "data": {"_v": 1, "sql": "{CUBE.base}"}}
    ]
    ds["fields"].append({"name": "base", "expression": expression("amount_base")})
    files, _ = export(model(ds))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    by_name(native["cubes"][0]["dimensions"])["base"]["sql"] = "adjusted_amount"
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    assert not yaml.safe_load(returned)["datasets"][0].get("metrics")
    assert any("field bindings changed" in i.detail for i in issues)


def test_older_origin_without_binding_record_is_preserved_natively():
    files, _ = export(model(dataset(metrics=[metric()])))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    measure = native["cubes"][0]["measures"][0]
    measure["meta"]["ossie"]["dataset_metric"].pop("bindings")
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert not back.get("metrics") and not back["datasets"][0].get("metrics")
    assert stash_of(back["datasets"][0])["extra_measures"][0]["measure"] == measure
    assert any("no field binding record" in i.detail for i in issues)


_NATIVE_SALES = """cubes:
- name: orders
  sql_table: samples.tpch.orders
  joins:
  - name: customers
    sql: "{CUBE}.o_custkey = {customers}.c_custkey"
    relationship: many_to_one
  dimensions:
  - {name: id, sql: o_orderkey, type: number, primary_key: true}
  - {name: status, sql: o_orderstatus, type: string}
  measures:
  - {name: count, type: count}
  - {name: revenue, sql: o_totalprice, type: sum}
  - {name: mixed, sql: "{revenue} + MAX({customers}.credit)", type: number}
- name: customers
  sql_table: samples.tpch.customer
  dimensions:
  - {name: c_custkey, sql: c_custkey, type: number, primary_key: true}
  - {name: name, sql: c_name, type: string}
views:
- name: sales
  cubes:
  - {join_path: orders, includes: [status, count, revenue, mixed]}
  - {join_path: orders.customers, includes: [name]}
"""


@pytest.mark.parametrize("project", [False, True])
def test_native_single_cube_measures_use_dataset_scope(project):
    files = {"model.yml": _NATIVE_SALES}
    imported = (convert_cube_view_to_ossie(files, "sales")[0]
                if project else convert_cube_to_ossie(files)[0])
    assert_ossie_is_valid(imported)
    document = yaml.safe_load(imported)
    orders = by_name(document["datasets"])["orders"]
    assert [m["name"] for m in orders["metrics"]] == ["count", "revenue"]
    revenue = orders["metrics"][1]["expression"]["dialects"][0]["expression"]
    assert revenue == "SUM(source.o_totalprice)"
    assert [m["name"] for m in document["metrics"]] == ["mixed"]
    assert "customers." in document["metrics"][0]["expression"]["dialects"][0]["expression"]

    exported, _ = convert_ossie_to_cube(imported)
    restored, _ = convert_cube_to_ossie(exported)
    assert_ossie_is_valid(restored)
    document = yaml.safe_load(restored)
    assert [m["name"] for m in by_name(document["datasets"])["orders"]["metrics"]] == [
        "count", "revenue",
    ]
    assert [m["name"] for m in document["metrics"]] == ["mixed"]


def test_authored_model_metric_keeps_scope_after_export():
    files, _ = export(model(dataset(), metrics=[{
        "name": "authored",
        "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                     "expression": "SUM(orders.amount)"}]},
    }]))
    restored, _ = convert_cube_to_ossie(files)
    document = yaml.safe_load(restored)
    assert document["metrics"][0]["name"] == "authored"
    assert not document["datasets"][0].get("metrics")


def test_cross_cube_formula_round_trip_keeps_native_primary_key_count():
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[2] / "interop/fixtures/cube/metric_bindings/model.yml"
    imported, _ = convert_cube_to_ossie({"model.yml": fixture.read_text()})
    exported, issues = convert_ossie_to_cube(imported)
    assert not list(issues)
    cubes = by_name(yaml.safe_load(exported["model.yml"])["cubes"])
    count = by_name(cubes["customer"]["measures"])["customer_count"]
    assert count["type"] == "count"
    assert "sql" not in count
    formula = by_name(cubes["sales"]["measures"])["revenue_per_customer"]
    assert formula["sql"] == "{CUBE.total_revenue} / {customer.customer_count}"


@pytest.mark.parametrize("function, arguments", [
    ("DATEDIFF", "{CUBE}.created_at, {CUBE}.shipped_at"),
    ("DATEADD", "1, {CUBE}.shipped_at"),
])
@pytest.mark.parametrize("dialect", [None, "ANSI_SQL"])
def test_recognized_date_unit_is_safe_at_dataset_scope_in_projection(function, arguments, dialect):
    native = yaml.safe_load(_NATIVE_SALES)
    orders = native["cubes"][0]
    orders["measures"] = [{"name": "days", "type": "number",
                           "sql": f"{function}(day, {arguments})"}]
    native["views"][0]["cubes"] = [{"join_path": "orders", "includes": ["days"]}]
    returned = convert_cube_view_to_ossie(
        {"model.yml": yaml.safe_dump(native)}, "sales", source_dialect=dialect)[0]
    document = yaml.safe_load(returned)
    assert not document.get("metrics")
    sql = document["datasets"][0]["metrics"][0]["expression"]["dialects"][0]["expression"]
    assert f"{function}(day," in sql
    assert "source.shipped_at" in sql
    assert "source.day" not in sql
    exported, _ = convert_ossie_to_cube(returned)
    native = parse_files(exported)
    rendered = yaml.safe_dump(native)
    assert f"{function}(day," in rendered


@pytest.mark.parametrize("reference", ["{net}", "{CUBE.net}", "{orders.net}"])
@pytest.mark.parametrize("dialect", [None, "SNOWFLAKE", "DATABRICKS"])
def test_native_dataset_metric_keeps_computed_dimension_reference(reference, dialect):
    native = yaml.safe_load(_NATIVE_SALES)
    orders = native["cubes"][0]
    orders["dimensions"].append({"name": "net", "sql": "o_totalprice - discount", "type": "number"})
    orders["measures"].extend([
        {"name": "net_sales", "type": "sum", "sql": reference},
        {"name": "raw_net_sales", "type": "sum", "sql": "net"},
        {"name": "double_net", "type": "number", "sql": "{net_sales} * 2"},
    ])
    returned, _ = convert_cube_to_ossie({"model.yml": yaml.safe_dump(native)}, source_dialect=dialect)
    metrics = by_name(by_name(yaml.safe_load(returned)["datasets"])["orders"]["metrics"])
    assert metrics["net_sales"]["expression"] == expression("SUM(net)", dialect or "ANSI_SQL")
    assert metrics["raw_net_sales"]["expression"] == expression("SUM(source.net)", dialect or "ANSI_SQL")
    assert metrics["double_net"]["expression"] == expression("SUM(net) * 2", dialect or "ANSI_SQL")
    exported, _ = convert_ossie_to_cube(returned)
    restored, _ = convert_cube_to_ossie(exported)
    assert by_name(by_name(yaml.safe_load(restored)["datasets"])["orders"]["metrics"])["net_sales"]["expression"] == expression("SUM(net)", dialect or "ANSI_SQL")


def test_native_dataset_identity_keeps_existing_view_on_its_original_measure():
    returned, _ = convert_cube_to_ossie({"model.yml": _NATIVE_SALES})
    document = yaml.safe_load(returned)
    document["metrics"].append(metric("revenue", "MAX(orders.id)"))
    files, _ = convert_ossie_to_cube(yaml.safe_dump(document), base_cube="orders")
    cubes = {}
    views = {}
    for content in files.values():
        native = yaml.safe_load(content)
        cubes.update(by_name(native.get("cubes")))
        views.update(by_name(native.get("views")))
    revenue = by_name(cubes["orders"]["measures"])["revenue"]
    assert revenue["type"] == "sum"
    assert revenue["sql"] == "{CUBE}.o_totalprice"
    assert "revenue" in views["sales"]["cubes"][0]["includes"]
    assert by_name(cubes["orders"]["measures"])["orders__revenue"]["type"] == "max"


def test_native_metric_collision_with_restored_local_name_stays_model_scoped():
    files, _ = export(model(dataset(metrics=[metric()]),
                            metrics=[metric("total_amount", "SUM(orders.id)")]))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    cube = native["cubes"][0]
    cube["measures"] = [m for m in cube["measures"]
                        if "dataset_metric" in (m.get("meta", {}).get("ossie") or {})]
    cube["measures"].append({"name": "total_amount", "type": "sum", "sql": "id"})
    files[path] = yaml.safe_dump(native)
    returned, _ = convert_cube_to_ossie(files)
    document = yaml.safe_load(returned)
    assert [m["name"] for m in document["datasets"][0]["metrics"]] == ["total_amount"]
    assert [m["name"] for m in document["metrics"]] == ["total_amount"]
    convert_ossie_to_cube(returned, base_cube="orders")


@pytest.mark.parametrize("kind", ["field", "model_metric"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("edit", ["sql", "type", "description", "unchanged"])
def test_restoring_saved_expression_warns_when_native_definition_changed(kind, legacy, edit):
    saved = expression("SUM(orders.id)" if kind == "model_metric" else "gross - tax")
    saved["sql"] = saved["dialects"][0]["expression"]
    if not legacy:
        saved.pop("dialects")
    ds = dataset()
    if kind == "field":
        ds["fields"][1]["expression"] = saved
    document = model(ds, metrics=[{"name": "total", "expression": saved}]
                     if kind == "model_metric" else [])
    files, _ = export(document)
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    cube = native["cubes"][0]
    member = by_name(cube["dimensions"])["amount"] if kind == "field" else cube["measures"][0]
    if edit == "sql":
        member["sql"] = "changed_value"
    elif edit == "type":
        member["type"] = "string" if kind == "field" else "max"
    elif edit == "description":
        member["description"] = "Updated explanation"
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    result = (by_name(back["datasets"][0]["fields"])["amount"] if kind == "field"
              else back["metrics"][0])
    assert result["expression"] == saved
    warnings = [i for i in issues if i.issue_type.value == "STALE_PARKED_EXPRESSION"]
    assert len(warnings) == (1 if edit in {"sql", "type"} else 0)
    if warnings:
        assert warnings[0].element_name == ("orders.amount" if kind == "field" else "orders.total")
        assert "restored the saved Ossie expression" in warnings[0].detail


def test_changed_dataset_measure_imports_current_definition_without_restoring_saved_sql():
    ds = dataset(metrics=[{"name": "total", "expression": {"sql": "SUM(id)"}}])
    files, _ = export(model(ds))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    native["cubes"][0]["measures"][0]["sql"] = "{CUBE.amount}"
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    back = yaml.safe_load(returned)
    assert not back["datasets"][0].get("metrics")
    assert back["metrics"][0]["expression"] == expression("SUM(orders.amount)", "OSSIE_SQL_2026")
    assert not [i for i in issues if i.issue_type.value == "STALE_PARKED_EXPRESSION"]


@pytest.mark.parametrize("primary", [False, True])
def test_saved_model_expression_warns_after_native_filter_edit(primary):
    saved = expression("SUM(CASE WHEN orders.id > 1 THEN orders.amount END)")
    if primary:
        saved = {"sql": saved["dialects"][0]["expression"]}
    else:
        saved["dialects"].append({"dialect": "DAX", "expression": "SUM(orders[amount])"})
    files, _ = export(model(dataset(), metrics=[{"name": "total", "expression": saved}]))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    measure = native["cubes"][0]["measures"][0]
    measure["filters"] = [{"sql": "{CUBE}.id > 2"}]
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    assert yaml.safe_load(returned)["metrics"][0]["expression"] == saved
    assert len(issues.of_type(IssueType.STALE_PARKED_EXPRESSION)) == 1


def test_older_export_without_expression_signature_keeps_restoration_compatibility():
    saved = {"sql": "SUM(orders.id)"}
    files, _ = export(model(dataset(), metrics=[{"name": "total", "expression": saved}]))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    native["cubes"][0]["measures"][0]["meta"]["ossie"].pop("expression_signature")
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    assert yaml.safe_load(returned)["metrics"][0]["expression"] == saved
    assert not issues.of_type(IssueType.STALE_PARKED_EXPRESSION)


def test_saved_primary_sql_warns_for_active_to_trialing_native_edit():
    ds = dataset()
    ds["fields"].append({"name": "status", "expression": {"sql": "status"}})
    saved = {"sql": "SUM(CASE WHEN orders.status = 'active' THEN orders.amount END)"}
    files, _ = export(model(ds, metrics=[{"name": "mrr", "expression": saved}]))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    measure = native["cubes"][0]["measures"][0]
    assert "'active'" in measure["sql"]
    measure["sql"] = measure["sql"].replace("'active'", "'trialing'")
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    assert yaml.safe_load(returned)["metrics"][0]["expression"] == saved
    assert [i.element_name for i in issues.of_type(IssueType.STALE_PARKED_EXPRESSION)] == ["orders.mrr"]


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("dataset_name", ["orders", "Order Items"])
@pytest.mark.parametrize("prefix", ["", " \n"])
def test_model_saved_expression_rebinds_sanitized_dataset_and_field_names(primary, dataset_name, prefix):
    ds = dataset(dataset_name)
    ds["fields"][1]["name"] = "Net Amount"
    owner = '"Order Items"' if dataset_name == "Order Items" else "orders"
    sql = prefix + f'SUM({owner}."Net Amount") + LENGTH(\'orders.Net Amount\')'
    saved = {"sql": sql} if primary else expression(sql)
    if not primary:
        saved["dialects"].append({"dialect": "DAX", "expression": "SUM(orders[Net Amount])"})
    files, _ = convert_ossie_to_cube(yaml.safe_dump(model(ds, metrics=[{"name": "total", "expression": saved}])))
    returned, _ = convert_cube_to_ossie(files)
    result = yaml.safe_load(returned)
    field = result["datasets"][0]["fields"][1]["name"]
    assert field == "net_amount"
    expr = result["metrics"][0]["expression"]
    restored = expr["sql"] if primary else expr["dialects"][0]["expression"]
    assert '"Net Amount"' not in restored
    assert '"net_amount"' in restored
    assert "'orders.Net Amount'" in restored
    if dataset_name == "Order Items":
        assert '"order_items".' in restored
    if not primary:
        assert expr["dialects"][1]["expression"] == "SUM(orders[Net Amount])"
    assert_ossie_is_valid(returned)


def test_warning_does_not_claim_a_consistently_edited_saved_expression_loses_the_edit():
    saved = {"sql": "SUM(CASE WHEN orders.id > 1 THEN orders.amount END)"}
    files, _ = export(model(dataset(), metrics=[{"name": "active", "expression": saved}]))
    path = "model/cubes/orders.yml"
    native = yaml.safe_load(files[path])
    measure = native["cubes"][0]["measures"][0]
    measure["sql"] = measure["sql"].replace("> 1", "> 2")
    measure["meta"]["ossie"]["expression"]["sql"] = saved["sql"].replace("> 1", "> 2")
    files[path] = yaml.safe_dump(native)
    returned, issues = convert_cube_to_ossie(files)
    assert "> 2" in yaml.safe_load(returned)["metrics"][0]["expression"]["sql"]
    warning = issues.of_type(IssueType.STALE_PARKED_EXPRESSION)[0].detail
    assert "not reflected" not in warning
    assert "remove meta.ossie.expression" in warning
    measure["meta"]["ossie"].pop("expression")
    files[path] = yaml.safe_dump(native)
    _, issues = convert_cube_to_ossie(files)
    assert not issues.of_type(IssueType.STALE_PARKED_EXPRESSION)


@pytest.mark.parametrize("native_type, sql", [
    ("number", "SUM({CUBE}.amount)"),
    ("count_distinct", "{CUBE}.id"),
    ("number", "COUNT(DISTINCT {CUBE}.id)"),
])
def test_default_dataset_metric_roundtrip_preserves_declared_native_type(native_type, sql):
    files = {"model.yml": yaml.safe_dump({"cubes": [{
        "name": "orders", "sql_table": "sales.public.orders",
        "dimensions": [
            {"name": "id", "sql": "id", "type": "number", "primary_key": True},
            {"name": "amount", "sql": "amount", "type": "number"},
        ],
        "measures": [{"name": "native", "type": native_type, "sql": sql}],
    }]})}
    for _ in range(3):
        imported, _ = convert_cube_to_ossie(files)
        local = yaml.safe_load(imported)["datasets"][0]["metrics"][0]
        assert stash_of(local).get("type") == native_type
        files, _ = convert_ossie_to_cube(imported)
        measure = yaml.safe_load(files["model.yml"])["cubes"][0]["measures"][0]
        assert measure["type"] == native_type
        assert "sql" in measure
