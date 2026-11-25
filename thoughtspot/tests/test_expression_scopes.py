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

"""Source-column and declared-field namespaces in exported ThoughtSpot formulas."""

from copy import deepcopy

import pytest
from ossie_thoughtspot import stash
from ossie_thoughtspot.constants import FIELD_STASH_FORMULA_ID
from ossie_thoughtspot.ossie_to_thoughtspot import convert
from ossie_thoughtspot.tml_to_ossie import convert as import_tml


def _expression(sql, dialect):
    return (
        {"sql": sql}
        if dialect == "sql"
        else {"dialects": [{"dialect": dialect, "expression": sql}]}
    )


def _model(dialect="sql"):
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "orders",
                "source": "db.sch.orders",
                "fields": [
                    {
                        "name": "amount",
                        "label": "Amount",
                        "datatype": "Decimal",
                        "expression": _expression("raw_amount", dialect),
                    },
                    {
                        "name": "absolute",
                        "datatype": "Decimal",
                        "expression": _expression("ABS(raw_amount)", dialect),
                    },
                ],
                "metrics": [
                    {"name": "total", "expression": _expression("SUM(amount)", dialect)}
                ],
            }
        ],
    }


def _formulas(result):
    return {f["name"]: f for f in result.documents.model.body.get("formulas", [])}


@pytest.mark.parametrize("dialect", ["sql", "ANSI_SQL", "OSSIE_SQL_2026"])
def test_dataset_metrics_and_computed_fields_use_separate_namespaces(dialect):
    model = _model(dialect)
    model["datasets"][0]["metrics"] += [
        {"name": "total_absolute", "expression": _expression("SUM(absolute)", dialect)},
        {
            "name": "total_raw",
            "expression": _expression("SUM(source.raw_amount)", dialect),
        },
    ]
    original = deepcopy(model)
    result = convert(model)
    formulas = _formulas(result)
    assert str(formulas["absolute"]["expr"]) == "abs ( [orders::Amount] )"
    assert str(formulas["total"]["expr"]) == "sum ( [orders::Amount] )"
    assert (
        str(formulas["total_absolute"]["expr"])
        == f"sum ( [{formulas['absolute']['id']}] )"
    )
    assert str(formulas["total_raw"]["expr"]) == "sum ( [orders::Amount] )"
    assert not result.issues.has_errors(), result.issues.as_dicts()
    assert model == original


def test_a_computed_field_shadows_the_same_named_source_column_in_a_metric():
    model = _model()
    ds = model["datasets"][0]
    ds["fields"][1]["name"] = "raw_amount"
    ds["metrics"] = [
        {"name": "computed", "expression": {"sql": "SUM(raw_amount)"}},
        {"name": "physical", "expression": {"sql": "SUM(source.raw_amount)"}},
    ]
    formulas = _formulas(convert(model))
    assert str(formulas["raw_amount"]["expr"]) == "abs ( [orders::Amount] )"
    assert (
        str(formulas["computed"]["expr"]) == f"sum ( [{formulas['raw_amount']['id']}] )"
    )
    assert str(formulas["physical"]["expr"]) == "sum ( [orders::Amount] )"


@pytest.mark.parametrize(
    "expression", ["SUM(AMOUNT)", 'SUM("AMOUNT")', "SUM( SOURCE.raw_amount )"]
)
def test_regular_and_quoted_local_references(expression):
    model = _model()
    model["datasets"][0]["metrics"][0]["expression"] = {"sql": expression}
    assert str(_formulas(convert(model))["total"]["expr"]) == "sum ( [orders::Amount] )"


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(raw_amount)",
        "SUM(other.amount)",
        "SUM(orders.amount)",
        'SUM("amount")',
        "SUM(missing)",
    ],
)
def test_a_metric_cannot_fall_back_to_another_namespace(expression):
    model = _model()
    model["datasets"][0]["metrics"][0]["expression"] = {"sql": expression}
    result = convert(model)
    assert "total" not in _formulas(result)
    assert any(i["code"] == "TS-EXPR-ANSI-UNRESOLVED" for i in result.issues.as_dicts())
    [issue] = [
        i
        for i in result.issues.as_dicts()
        if i["code"] == "TS-MODEL-METRIC-UNTRANSLATABLE"
    ]
    assert issue["object_ref"] == "metric:orders.total"


def test_a_field_named_source_disables_the_reserved_qualifier():
    model = _model()
    model["datasets"][0]["fields"].append(
        {
            "name": "source",
            "datatype": "Decimal",
            "expression": {"sql": "other_raw"},
        }
    )
    model["datasets"][0]["metrics"] = [
        {"name": "local", "expression": {"sql": "SUM(source)"}},
        {"name": "raw", "expression": {"sql": "SUM(source.raw_amount)"}},
    ]
    formulas = _formulas(convert(model))
    assert str(formulas["local"]["expr"]) == "sum ( [orders::source] )"
    assert "raw" not in formulas


def test_same_named_metrics_from_two_datasets_have_distinct_ids_and_correct_operands():
    model = _model()
    other = deepcopy(model["datasets"][0])
    other.update(name="refunds", source="db.sch.refunds")
    model["datasets"].append(other)
    result = convert(model)
    formulas = list(_formulas(result).values())
    assert len({f["id"] for f in formulas}) == len(formulas)
    totals = [str(f["expr"]) for f in formulas if f["name"].startswith("total")]
    assert totals == ["sum ( [orders::Amount] )", "sum ( [refunds::Amount] )"]
    assert any(
        i["code"] == "TS-MODEL-DISPLAY-NAME-COLLISION" for i in result.issues.as_dicts()
    )


def test_a_dataset_metric_preserved_id_is_reserved_before_new_fields():
    model = _model()
    metric = model["datasets"][0]["metrics"][0]
    model["datasets"][0]["metrics"][0] = stash.write_stash(
        metric,
        {FIELD_STASH_FORMULA_ID: "formula_absolute"},
    )
    formulas = _formulas(convert(model))
    assert formulas["total"]["id"] == "formula_absolute"
    assert formulas["absolute"]["id"] != "formula_absolute"


def test_native_formula_wins_over_portable_context_and_survives_an_import_export():
    model = _model()
    metric = model["datasets"][0]["metrics"][0]
    metric["expression"] = {
        "sql": "SUM(missing)",
        "dialects": [
            {"dialect": "THOUGHTSPOT", "expression": "sum ( [orders::Amount] )"},
        ],
    }
    exported = convert(model)
    imported = import_tml(exported.documents)
    exported_again = convert(imported.model)
    assert str(_formulas(exported)["total"]["expr"]) == "sum ( [orders::Amount] )"
    assert any(
        str(f["expr"]) == "sum ( [orders::Amount] )"
        for f in _formulas(exported_again).values()
    )


def test_unknown_source_columns_and_unsupported_formulas_are_reported():
    model = _model()
    model["datasets"][0]["fields"][1]["expression"] = {"sql": "ABS(unknown_raw)"}
    model["datasets"][0]["metrics"] = [
        {"name": "missing_field", "expression": {"sql": "SUM(absolute)"}},
        {"name": "unsupported", "expression": {"sql": "SUM(amount) + 1"}},
    ]
    result = convert(model)
    assert not _formulas(result)
    assert result.issues.has_errors()


def test_model_metric_names_still_resolve_in_the_global_field_namespace():
    model = _model()
    model["metrics"] = [
        {"name": "global", "expression": {"sql": "SUM(orders.absolute)"}}
    ]
    formulas = _formulas(convert(model))
    assert str(formulas["global"]["expr"]) == f"sum ( [{formulas['absolute']['id']}] )"


@pytest.mark.parametrize("quote", ['"', ""])
def test_source_columns_keep_exact_warehouse_spelling_for_quoted_reads(quote):
    model = _model()
    model["datasets"][0]["fields"][0]["expression"] = {"sql": '"raw_amount"'}
    model["datasets"][0]["fields"][1]["expression"] = {
        "sql": f"ABS({quote}raw_amount{quote})"
    }
    model["datasets"][0]["metrics"] = [
        {
            "name": "raw",
            "expression": {"sql": f"SUM(source.{quote}raw_amount{quote})"},
        }
    ]
    formulas = _formulas(convert(model))
    assert str(formulas["absolute"]["expr"]) == "abs ( [orders::Amount] )"
    assert str(formulas["raw"]["expr"]) == "sum ( [orders::Amount] )"


def test_nested_scalar_templates_group_the_entire_translated_argument():
    model = _model()
    model["datasets"][0]["fields"][1]["expression"] = {"sql": "ABS(ABS(raw_amount))"}
    expression = str(_formulas(convert(model))["absolute"]["expr"])
    assert expression == "abs ( (abs ( [orders::Amount] )) )"


def test_scalar_rows_with_unverified_null_semantics_stay_unsupported():
    model = _model()
    model["datasets"][0]["fields"][1]["expression"] = {"sql": "SIGN(raw_amount)"}
    result = convert(model)
    assert "absolute" not in _formulas(result)
    assert "total" in _formulas(result)
    assert any(i["code"] == "TS-EXPR-ANSI-UNMATCHED" for i in result.issues.as_dicts())


def test_multiple_aliases_of_the_same_source_column_are_one_physical_binding():
    model = _model()
    model["datasets"][0]["fields"].append(
        {
            "name": "second_amount",
            "label": "Second Amount",
            "datatype": "Decimal",
            "expression": {"sql": "raw_amount"},
        }
    )
    model["datasets"][0]["metrics"].append(
        {
            "name": "physical",
            "expression": {"sql": "SUM(source.raw_amount)"},
        }
    )
    result = convert(model)
    formulas = _formulas(result)
    assert str(formulas["absolute"]["expr"]) == "abs ( [orders::Amount] )"
    assert str(formulas["physical"]["expr"]) == "sum ( [orders::Amount] )"
    assert not result.issues.has_errors(), result.issues.as_dicts()


def test_unquoted_source_reads_cannot_choose_between_case_sensitive_columns():
    model = _model()
    model["datasets"][0]["fields"].append(
        {
            "name": "other",
            "datatype": "Decimal",
            "expression": {"sql": '"RAW_AMOUNT"'},
        }
    )
    result = convert(model)
    assert "absolute" not in _formulas(result)
    assert result.issues.has_errors()
