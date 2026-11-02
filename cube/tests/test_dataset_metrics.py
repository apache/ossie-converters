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
        "version": "0.2.0.dev0",
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


def test_local_metric_is_renamed_when_a_model_metric_already_uses_the_cube_member():
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
    assert any("sanitization would collide" in issue.detail for issue in issues)
    returned, _ = convert_cube_to_ossie(files)
    assert_ossie_is_valid(returned, "sanitized local namespace")
    restored = yaml.safe_load(returned)["datasets"][0]
    names = [m["name"] for m in restored["metrics"]]
    assert len(set(names)) == 2
    assert "gross_amount" not in names
    assert parse_files(export(yaml.safe_load(returned))[0]) == parse_files(files)


def test_explicit_native_overrides_survive_local_metric_import():
    local = metric()
    local["custom_extensions"] = [
        {
            "vendor_name": "CUBE",
            "data": '{"_v": 1, "type": "number", "sql": "SUM({CUBE.amount})"}',
        }
    ]
    files, _ = export(model(dataset(metrics=[local])))
    returned, _ = convert_cube_to_ossie(files)
    restored = yaml.safe_load(returned)["datasets"][0]["metrics"][0]
    assert stash_of(restored)["type"] == "number"
    assert stash_of(restored)["sql"] == "SUM({CUBE.amount})"
    assert parse_files(export(yaml.safe_load(returned))[0]) == parse_files(files)


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
    normalized_sql = sql.replace(f"{quote}Net Amount{quote}", "net_amount")
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
def test_dataset_scope_rejects_undeclared_or_nonlocal_references(sql, match):
    with pytest.raises(ConversionError, match=match):
        export(model(dataset(metrics=[metric("invalid", sql)])))


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


def test_projection_publishes_a_dataset_measure_as_a_model_metric():
    files, _ = export(model(dataset(metrics=[metric()])))
    projected, source, _ = convert_cube_view_to_ossie(files, "shop")
    document = yaml.safe_load(projected)
    assert source == "orders"
    assert document["metrics"][0]["name"] == "total_amount"
    assert (
        "orders." in document["metrics"][0]["expression"]["dialects"][0]["expression"]
    )
    assert not document["datasets"][0].get("metrics")


def test_primary_key_count_and_multiple_dialects_survive_repeated_round_trips():
    local = metric("order_count", "COUNT(DISTINCT id)")
    local["expression"]["dialects"].append(
        {"dialect": "SNOWFLAKE", "expression": "COUNT(DISTINCT ID)"}
    )
    local["custom_extensions"] = [
        {"vendor_name": "COMMON", "data": '{"unit": "orders"}'}
    ]
    files, _ = export(model(dataset(metrics=[local])))
    assert measures(files)["order_count"]["type"] == "count"
    assert "sql" not in measures(files)["order_count"]
    for _ in range(3):
        returned, _ = convert_cube_to_ossie(files)
        back = yaml.safe_load(returned)
        restored = back["datasets"][0]["metrics"][0]
        assert restored["expression"] == local["expression"]
        assert local["custom_extensions"][0] in restored["custom_extensions"]
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
