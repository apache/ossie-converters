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

"""Warehouse field formulas use the physical source, not the modeled column."""

from copy import deepcopy

import pytest
from ossie_sigma.ossie_to_sigma import OssieToSigmaConverter
from ossie_sigma.sigma_to_ossie import SigmaToOssieConverter

from ossie import OssieSemanticModel


def _model(expression="calendar_date", dialect="sql"):
    expr = (
        {"sql": expression}
        if dialect == "sql"
        else {"dialects": [{"dialect": dialect, "expression": expression}]}
    )
    return {
        "version": "0.3.0.dev0",
        "name": "calendar",
        "datasets": [
            {
                "name": "calendar_alias",
                "source": "db.schema.dim_date_vw",
                "fields": [
                    {"name": "calendar_date", "datatype": "Date", "expression": expr}
                ],
            }
        ],
    }


def _export(model):
    result = OssieToSigmaConverter().convert(OssieSemanticModel.model_validate(model))
    assert not result.issues, result.issues
    return result.output


def _element(spec):
    return spec["pages"][0]["elements"][0]


@pytest.mark.parametrize(
    "dialect,expression",
    [
        ("sql", "calendar_date"),
        ("ANSI_SQL", "calendar_date"),
        ("OSSIE_SQL_2026", "calendar_date"),
        ("SNOWFLAKE", '"calendar_date"'),
        ("DATABRICKS", "`calendar_date`"),
        ("BIGQUERY", "`calendar_date`"),
        ("sql", "calendar_alias.calendar_date"),
        ("sql", "source.calendar_date"),
        ("sql", "dim_date_vw.calendar_date"),
    ],
)
def test_warehouse_field_qualifies_with_physical_table_even_when_dataset_is_aliased(
    dialect, expression
):
    element = _element(_export(_model(expression, dialect)))
    assert element["source"]["path"] == ["db", "schema", "dim_date_vw"]
    assert element["name"] == "calendar_alias"
    assert element["columns"][0]["formula"] == "[dim_date_vw/calendar_date]"


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("calendar_date", "[dim_date_vw/calendar_date]"),
        ("calendar_alias.calendar_date", "[dim_date_vw/calendar_date]"),
        ("calendar_date + 1", "([dim_date_vw/calendar_date] + 1)"),
        (
            "COALESCE(calendar_date, calendar_date)",
            "IfNull([dim_date_vw/calendar_date], [dim_date_vw/calendar_date])",
        ),
    ],
)
def test_field_alias_and_computed_inputs_stay_bound_to_source_columns(
    expression, expected
):
    model = _model(expression)
    model["datasets"][0]["fields"][0]["name"] = "renamed"
    column = _element(_export(model))["columns"][0]
    assert column["name"] == "renamed"
    assert column["formula"] == expected


def test_metric_references_stay_in_the_modeled_column_namespace():
    model = _model()
    model["metrics"] = [
        {
            "name": "count_dates",
            "expression": {"sql": "COUNT(calendar_alias.calendar_date)"},
        }
    ]
    element = _element(_export(model))
    assert element["columns"][0]["formula"] == "[dim_date_vw/calendar_date]"
    assert element["metrics"][0]["formula"] == "Count([calendar_date])"


@pytest.mark.parametrize("kind", ["sql", "table", "data-model", "join", "union"])
def test_non_warehouse_sources_keep_their_existing_formula_context(kind):
    model = _model()
    source = {"kind": kind, "id": "native-source", "sql": "SELECT calendar_date FROM t"}
    model["datasets"][0]["nonportable_extensions"] = [
        {"vendor_name": "SIGMA", "data": {"source": source}}
    ]
    element = _element(_export(model))
    assert element["source"] == source
    assert element["columns"][0]["formula"] == "[calendar_date]"


def test_native_sigma_text_and_native_column_overrides_keep_precedence():
    model = _model("missing_column")
    field = model["datasets"][0]["fields"][0]
    field["expression"]["dialects"] = [
        {"dialect": "SIGMA", "expression": "[Native/Date]"}
    ]
    assert _element(_export(model))["columns"][0]["formula"] == "[Native/Date]"
    field["nonportable_extensions"] = [
        {"vendor_name": "SIGMA", "data": {"native": {"formula": "[Override/Date]"}}}
    ]
    assert _element(_export(model))["columns"][0]["formula"] == "[Override/Date]"


def test_generated_reference_survives_round_trip_with_only_portable_sql_remaining():
    spec = _export(_model())
    imported = SigmaToOssieConverter().convert(spec).output
    document = imported.model_dump(mode="json", exclude_none=True)
    field = document["datasets"][0]["fields"][0]
    portable = next(
        e["expression"]
        for e in field["expression"]["dialects"]
        if e["dialect"] == "ANSI_SQL"
    )
    field["expression"] = {"sql": portable}
    field.pop("nonportable_extensions", None)
    assert (
        _element(_export(document))["columns"][0]["formula"]
        == "[dim_date_vw/calendar_date]"
    )


def test_conversion_does_not_mutate_the_callers_model():
    model = _model()
    original = deepcopy(model)
    assert (
        _element(_export(model))["columns"][0]["formula"]
        == "[dim_date_vw/calendar_date]"
    )
    assert model == original


@pytest.mark.parametrize("expression", ["calendar_date + 1", "ABS(calendar_date)"])
def test_a_computed_field_keeps_its_name_when_an_input_has_the_same_name(expression):
    model = _model(expression)
    model["metrics"] = [
        {
            "name": "date_count",
            "expression": {"sql": "COUNT(calendar_alias.calendar_date)"},
        }
    ]
    spec = _export(model)
    element = _element(spec)
    assert element["columns"][0]["name"] == "calendar_date"
    assert element["metrics"][0]["formula"] == "Count([calendar_date])"
    imported = SigmaToOssieConverter().convert(spec).output
    assert imported.datasets[0].fields[0].name == "calendar_date"
    assert (
        _element(OssieToSigmaConverter().convert(imported).output)["columns"][0]["name"]
        == "calendar_date"
    )


def test_a_long_calculation_does_not_reenter_the_recursive_sigma_parser():
    model = _model(" + ".join(["calendar_date"] * 120))
    column = _element(_export(model))["columns"][0]
    assert column["name"] == "calendar_date"
    assert column["formula"].count("[dim_date_vw/calendar_date]") == 120
