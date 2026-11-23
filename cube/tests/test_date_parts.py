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

"""Date units stay keywords while date operands become Cube column references."""

import pytest
import yaml
from _util import by_name

from ossie_cube import convert_cube_to_ossie, convert_ossie_to_cube
from ossie_cube.expressions import unqualified_column_names


def _expression(text, dialect):
    if dialect == "sql":
        return {"sql": text}
    return {"dialects": [{"dialect": dialect, "expression": text}]}


def _model(text, dialect, scope):
    dataset = {
        "name": "ticket",
        "source": "support.public.ticket",
        "primary_key": ["id"],
        "fields": [
            {
                "name": name,
                "datatype": datatype,
                "expression": _expression(name, dialect),
            }
            for name, datatype in [
                ("id", "Integer"),
                ("created_at", "DateTime"),
                ("resolved_at", "DateTime"),
                ("day", "DateTime"),
            ]
        ],
    }
    model = {"version": "0.3.0.dev0", "name": "support", "datasets": [dataset]}
    item = {
        "name": "elapsed",
        "datatype": "Integer",
        "expression": _expression(text, dialect),
    }
    if scope == "field":
        dataset["fields"].append(item)
    else:
        owner = dataset if scope == "dataset" else model
        owner["metrics"] = [item]
    return model


@pytest.mark.parametrize(
    "dialect", ["sql", "OSSIE_SQL_2026", "ANSI_SQL", "SNOWFLAKE", "DATABRICKS"]
)
@pytest.mark.parametrize("scope", ["field", "model", "dataset"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "DATEDIFF(day, created_at, resolved_at)",
            "DATEDIFF(day, {CUBE}.created_at, {CUBE}.resolved_at)",
        ),
        ("DATEADD(day, 7, created_at)", "DATEADD(day, 7, {CUBE}.created_at)"),
        (
            "DATEDIFF(day, created_at, '2024-12-31')",
            "DATEDIFF(day, {CUBE}.created_at, '2024-12-31')",
        ),
        (
            "DATEADD(day, -1, DATEADD(month, 1, created_at))",
            "DATEADD(day, -1, DATEADD(month, 1, {CUBE}.created_at))",
        ),
        (
            "DATEADD(day, DATEDIFF(day, created_at, resolved_at), created_at)",
            "DATEADD(day, DATEDIFF(day, {CUBE}.created_at, {CUBE}.resolved_at), {CUBE}.created_at)",
        ),
        (
            "DATEDIFF(day, created_at, day)",
            "DATEDIFF(day, {CUBE}.created_at, {CUBE}.day)",
        ),
        ("DATEADD(day, 7, day)", "DATEADD(day, 7, {CUBE}.day)"),
    ],
)
def test_date_operands_are_qualified_without_qualifying_the_unit(
    dialect, scope, text, expected
):
    if scope == "model":
        # Model metrics bind declared fields with dataset-qualified names.
        for name in ["created_at", "resolved_at"]:
            text = text.replace(name, f"ticket.{name}")
        if text.endswith(", day)"):
            text = text[:-4] + "ticket.day)"
    if scope != "field":
        text = f"AVG({text})"
    model = _model(text, dialect, scope)
    files, _ = convert_ossie_to_cube(
        yaml.safe_dump(model), dialect=None if dialect == "sql" else dialect
    )
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    member = by_name(cube["dimensions" if scope == "field" else "measures"])["elapsed"]
    if scope != "field":
        assert member["type"] == "avg"
    # Check the native SQL, since parked metadata can hide a broken export on reimport.
    assert member["sql"] == expected
    restored, _ = convert_cube_to_ossie(files)
    owner = yaml.safe_load(restored)
    if scope != "model":
        owner = owner["datasets"][0]
    item = by_name(owner["fields" if scope == "field" else "metrics"])["elapsed"]
    assert item["expression"] == _expression(text, dialect)


@pytest.mark.parametrize(
    "dialect", [None, "ANSI_SQL", "OSSIE_SQL_2026", "SNOWFLAKE", "DATABRICKS"]
)
def test_unit_is_not_reported_as_a_column(dialect):
    assert unqualified_column_names(
        "DATEDIFF(day, created_at, resolved_at)", dialect
    ) == {"created_at", "resolved_at"}
    assert unqualified_column_names("DATEADD(day, 7, created_at)", dialect) == {
        "created_at"
    }


def test_native_bigquery_keeps_its_unit_last_argument_order():
    model = _model("DATE_DIFF(day, created_at, DAY)", "BIGQUERY", "field")
    files, _ = convert_ossie_to_cube(yaml.safe_dump(model), dialect="BIGQUERY")
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    assert by_name(cube["dimensions"])["elapsed"]["sql"] == (
        "DATE_DIFF({CUBE}.day, {CUBE}.created_at, DAY)"
    )


@pytest.mark.parametrize("model_scope", [False, True])
def test_import_without_a_source_dialect_preserves_the_date_unit(model_scope):
    files = {
        "model.yml": yaml.safe_dump(
            {
                "cubes": [
                    {
                        "name": "ticket",
                        "sql_table": "support.public.ticket",
                        "dimensions": [
                            {
                                "name": "id",
                                "type": "number",
                                "sql": "id",
                                "primary_key": True,
                            }
                        ],
                        "measures": [
                            {
                                "name": "elapsed",
                                "type": "avg",
                                "sql": "DATEDIFF(day, created_at, resolved_at)",
                            }
                        ],
                    }
                ]
            }
        )
    }
    returned, _ = convert_cube_to_ossie(files, scope_native_metrics=not model_scope)
    document = yaml.safe_load(returned)
    metric = (document if model_scope else document["datasets"][0])["metrics"][0]
    qualifier = "ticket" if model_scope else "source"
    assert metric["expression"]["dialects"][0]["expression"] == (
        f"AVG(DATEDIFF(day, {qualifier}.created_at, {qualifier}.resolved_at))"
    )


def test_composite_metric_glue_does_not_resolve_a_date_unit_as_a_field():
    model = _model("DATEADD(day, SUM(ticket.id), MAX(day))", "sql", "model")
    # The column named day belongs to another cube, so the aggregates are decomposed.
    model["datasets"][0]["fields"] = [
        f for f in model["datasets"][0]["fields"] if f["name"] != "day"
    ]
    model["datasets"].append(
        {
            "name": "calendar",
            "source": "support.public.calendar",
            "fields": [
                {"name": "day", "datatype": "DateTime", "expression": {"sql": "day"}}
            ],
        }
    )
    files, _ = convert_ossie_to_cube(yaml.safe_dump(model), base_cube="ticket")
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    sql = by_name(cube["measures"])["elapsed"]["sql"]
    assert sql.startswith("DATEADD(day, ")
    assert "{calendar}.day" not in sql


@pytest.mark.parametrize("prefix", ["", " ", "\n"])
@pytest.mark.parametrize(
    "tail", ["revenue", "(revenue)", "tax_rate", "(tax_rate)", "day"]
)
def test_composite_metric_tail_keeps_its_whole_expression_bindings(prefix, tail):
    model = _model(
        f"{prefix}SUM(ticket.id) / COUNT(DISTINCT calendar.id) * {tail}", "sql", "model"
    )
    model["datasets"][0]["fields"].append(
        {"name": "tax_rate", "datatype": "Decimal", "expression": {"sql": "tax_rate"}}
    )
    model["datasets"].append(
        {
            "name": "calendar",
            "source": "support.public.calendar",
            "fields": [
                {"name": "id", "datatype": "Integer", "expression": {"sql": "id"}}
            ],
        }
    )
    model["metrics"].append(
        {"name": "revenue", "expression": {"sql": "SUM(ticket.id)"}}
    )
    files, _ = convert_ossie_to_cube(yaml.safe_dump(model), base_cube="ticket")
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    sql = by_name(cube["measures"])["elapsed"]["sql"]
    expected = (
        tail.replace("revenue", "{revenue}")
        .replace("tax_rate", "{CUBE}.tax_rate")
        .replace("day", "{CUBE}.day")
    )
    assert sql.endswith(" * " + expected)


@pytest.mark.parametrize("function", ["DATEADD", "DATEDIFF"])
def test_databricks_two_argument_date_call_keeps_its_first_operand(function):
    text = f"{function}(day, SUM(ticket.id)) + MAX(calendar.day)"
    model = _model(text, "DATABRICKS", "model")
    model["datasets"][0]["fields"] = [
        f for f in model["datasets"][0]["fields"] if f["name"] != "day"
    ]
    model["metrics"].append(
        {
            "name": "day",
            "expression": _expression("MAX(ticket.created_at)", "DATABRICKS"),
        }
    )
    model["datasets"].append(
        {
            "name": "calendar",
            "source": "support.public.calendar",
            "fields": [
                {"name": "day", "datatype": "DateTime", "expression": {"sql": "day"}}
            ],
        }
    )
    files, _ = convert_ossie_to_cube(
        yaml.safe_dump(model), base_cube="ticket", dialect="DATABRICKS"
    )
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    sql = by_name(cube["measures"])["elapsed"]["sql"]
    assert sql.startswith(function + "({day}, ")


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "CONCAT('__ossie_ref_0__', username)",
            "CONCAT('__ossie_ref_0__', {CUBE}.username)",
        ),
        (
            "CONCAT('__ossie_ref_0__', ticket.created_at, username)",
            "CONCAT('__ossie_ref_0__', {CUBE}.created_at, {CUBE}.username)",
        ),
    ],
)
def test_literal_that_looks_like_a_reference_marker_is_preserved(text, expected):
    files, _ = convert_ossie_to_cube(yaml.safe_dump(_model(text, "sql", "field")))
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    assert by_name(cube["dimensions"])["elapsed"]["sql"] == expected


def test_bigquery_dotted_path_with_a_unit_named_head_keeps_its_cube_reference():
    model = _model(
        "SUM(ticket.id) / COUNT(calendar.id) * day.`payload.tax_rate`",
        "BIGQUERY",
        "model",
    )
    for name, field in [("calendar", "id"), ("day", "payload")]:
        model["datasets"].append(
            {
                "name": name,
                "source": f"support.public.{name}",
                "fields": [
                    {"name": field, "datatype": "Integer", "expression": {"sql": field}}
                ],
            }
        )
    files, _ = convert_ossie_to_cube(
        yaml.safe_dump(model), base_cube="ticket", dialect="BIGQUERY"
    )
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    assert by_name(cube["measures"])["elapsed"]["sql"].endswith(
        " * {day}.`payload.tax_rate`"
    )


def test_composite_unit_marker_cannot_overwrite_a_real_member():
    model = _model(
        "DATEADD(day, SUM(ticket.id), ticket.__ossie_unit_8__) = MAX(calendar.day)",
        "sql",
        "model",
    )
    model["datasets"][0]["fields"].append(
        {
            "name": "__ossie_unit_8__",
            "datatype": "DateTime",
            "expression": {"sql": "created_at"},
        }
    )
    model["datasets"].append(
        {
            "name": "calendar",
            "source": "support.public.calendar",
            "fields": [
                {"name": "day", "datatype": "DateTime", "expression": {"sql": "day"}}
            ],
        }
    )
    files, _ = convert_ossie_to_cube(yaml.safe_dump(model), base_cube="ticket")
    cube = yaml.safe_load(files["model/cubes/ticket.yml"])["cubes"][0]
    sql = by_name(cube["measures"])["elapsed"]["sql"]
    assert sql.startswith("DATEADD(day, ")
    assert "{CUBE.__ossie_unit_8__}" in sql
