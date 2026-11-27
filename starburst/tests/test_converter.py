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

from pathlib import Path

import pytest
import yaml
from ossie_starburst import (
    ConversionError,
    convert_ossie_to_starburst,
    convert_starburst_to_ossie,
)
from ossie_starburst.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "sales.yaml"


def test_context_studio_round_trip():
    original = yaml.safe_load(FIXTURE.read_text())
    ossie = convert_starburst_to_ossie(FIXTURE.read_text())
    model = yaml.safe_load(ossie)
    orders, buyers = model["datasets"]
    assert model["name"] == "Commerce Sales"
    assert orders["source"] == "commerce.sales.orders"
    assert orders["fields"][0]["ai_context"] == {"synonyms": ["net sales"]}
    assert orders["metrics"][0]["expression"]["sql"] == "SUM(source.net_amount)"
    assert buyers["source"] == "commerce.sales.buyers"
    assert model["relationships"][0]["from"] == "orders"
    assert model["relationships"][0]["from_columns"] == ["buyer_key", "tenant_key"]

    roundtrip = yaml.safe_load(convert_ossie_to_starburst(ossie))
    assert roundtrip["joins"] == original["joins"]
    assert roundtrip["semantics"] == original["semantics"]
    schema = roundtrip["catalogs"][0]["schemas"][0]
    assert (
        schema["tables"][0]["columns"]
        == original["catalogs"][0]["schemas"][0]["tables"][0]["columns"]
    )
    assert schema["views"][0]["columns"][0]["description"] == "Stable buyer identifier"
    assert roundtrip["catalogs"][0]["summary"] == "Commerce catalog"


def test_export_resolves_aliases_and_calculated_fields():
    model = {
        "version": "0.3.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": 'iceberg."sales.region".orders',
                "fields": [
                    {"name": "quantity", "expression": {"sql": "units"}},
                    {
                        "name": "net",
                        "expression": {"sql": "amount - discount"},
                        "label": "Net amount",
                    },
                ],
                "metrics": [
                    {
                        "name": "gross",
                        "expression": {"sql": "SUM(net * quantity + source.tax)"},
                    }
                ],
            }
        ],
        "metrics": [
            {"name": "total_units", "expression": {"sql": "SUM(orders.quantity)"}}
        ],
    }
    result = yaml.safe_load(
        convert_ossie_to_starburst(yaml.safe_dump(model), domain="Commerce")
    )
    assert result["semantics"]["domain"] == "Commerce"
    assert [metric["sql"] for metric in result["semantics"]["metrics"]] == [
        "SUM((amount - discount) * units + tax)",
        "SUM(units)",
    ]
    assert result["semantics"]["dimensions"][1]["label"] == "Net amount"
    assert (
        result["semantics"]["dimensions"][1]["table"] == r"iceberg.sales\.region.orders"
    )


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SUM(orders.amount) + SUM(buyers.amount)", "exactly one identifiable dataset"),
        ("SUM(orders.amount) / 100", "explicit TRINO expression"),
        ("AGG(orders.revenue)", "no Context Studio equivalent"),
    ],
)
def test_unsupported_metric_reports_reason(sql, message):
    model = {
        "version": "0.3.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": table,
                "source": f"iceberg.sales.{table}",
                "fields": [{"name": "amount", "expression": {"sql": "amount"}}],
            }
            for table in ("orders", "buyers")
        ],
        "metrics": [{"name": "bad_metric", "expression": {"sql": sql}}],
    }
    with pytest.raises(ConversionError, match=message):
        convert_ossie_to_starburst(yaml.safe_dump(model))


def test_unrepresentable_native_entries_are_restored():
    original = yaml.safe_load(FIXTURE.read_text())
    original["joins"][0]["cardinality"] = "MANY_TO_MANY"
    original["semantics"]["metrics"].append(
        {"name": "unbound_total", "sql": "COUNT(*)"}
    )
    with pytest.warns(UserWarning) as notices:
        ossie = convert_starburst_to_ossie(yaml.safe_dump(original))
    assert len(notices) == 2
    assert "relationships" not in yaml.safe_load(ossie)
    restored = yaml.safe_load(convert_ossie_to_starburst(ossie))
    assert restored["joins"] == original["joins"]
    assert (
        sorted(restored["semantics"]["metrics"], key=lambda entry: entry["name"])
        == original["semantics"]["metrics"]
    )


def test_reference_only_join_does_not_create_native_annotations():
    original = yaml.safe_load(FIXTURE.read_text())
    original.pop("catalogs")
    original.pop("semantics")
    ossie = yaml.safe_load(convert_starburst_to_ossie(yaml.safe_dump(original)))
    assert len(ossie["datasets"]) == 2
    native = yaml.safe_load(convert_ossie_to_starburst(yaml.safe_dump(ossie)))
    assert not native["catalogs"]
    assert native["joins"] == original["joins"]


def test_native_references_preserve_literal_dots_and_backslashes():
    original = yaml.safe_load(FIXTURE.read_text())
    original["catalogs"][0]["schemas"][0]["name"] = "sales.region"
    original["catalogs"][0]["schemas"][0]["tables"][0]["name"] = r"order\history"
    table = r"commerce.sales\.region.order\\history"
    for kind in ("metrics", "dimensions"):
        original["semantics"][kind][0]["table"] = table
    original.pop("joins")
    ossie = convert_starburst_to_ossie(yaml.safe_dump(original))
    assert (
        yaml.safe_load(ossie)["datasets"][0]["source"]
        == r'commerce."sales.region"."order\history"'
    )
    restored = yaml.safe_load(convert_ossie_to_starburst(ossie))
    assert restored["semantics"]["metrics"][0]["table"] == table


def test_skip_unsupported_metrics_keeps_convertible_columns_and_metrics():
    model = yaml.safe_load(convert_starburst_to_ossie(FIXTURE.read_text()))
    orders = model["datasets"][0]
    orders["fields"].append({"name": "comment", "expression": {"sql": "comment"}})
    orders["metrics"].append(
        {"name": "bad_metric", "expression": {"sql": "SUM(source.amount) / 100"}}
    )
    with pytest.warns(UserWarning, match="skipping metric bad_metric"):
        native = yaml.safe_load(
            convert_ossie_to_starburst(
                yaml.safe_dump(model), skip_unsupported_metrics=True
            )
        )
    assert [entry["name"] for entry in native["semantics"]["metrics"]] == [
        "total_sales"
    ]
    assert (
        native["catalogs"][0]["schemas"][0]["tables"][0]["columns"][-1]["name"]
        == "comment"
    )


def test_cli_writes_yaml_and_rejects_invalid_input(tmp_path, capsys):
    converted = tmp_path / "ossie.yaml"
    assert (
        main(["import", "-i", str(FIXTURE), "-o", str(converted), "--name", "sales"])
        == 0
    )
    assert yaml.safe_load(converted.read_text())["name"] == "sales"
    assert main(["export", "-i", str(converted), "--domain", "Analytics"]) == 0
    output = capsys.readouterr()
    assert yaml.safe_load(output.out)["semantics"]["domain"] == "Analytics"
    assert not output.err

    converted.write_text("version: duplicate\nversion: duplicate\n")
    assert main(["export", "-i", str(converted)]) == 1
    assert "duplicate mapping key" in capsys.readouterr().err


def test_reversed_composite_join_is_duplicate():
    native = yaml.safe_load(FIXTURE.read_text())
    join = native["joins"][0]
    native["joins"].append(
        {
            "name": "orders_to_buyers",
            "from": list(reversed(join["to"])),
            "to": list(reversed(join["from"])),
            "cardinality": "MANY_TO_ONE",
        }
    )
    with pytest.raises(ConversionError, match="duplicate join column pairs"):
        convert_starburst_to_ossie(yaml.safe_dump(native))


def test_import_metric_resolves_declared_columns_when_source_is_shadowed():
    native = yaml.safe_load(FIXTURE.read_text())
    semantics = native["semantics"]
    table = semantics["metrics"][0]["table"]
    semantics["dimensions"].append(
        {"name": "source", "table": table, "sql": "tenant_key"}
    )
    semantics["metrics"].append(
        {"name": "total_units", "table": table, "sql": "SUM(units)"}
    )
    with pytest.warns(UserWarning, match="total_units.*source.*preserved"):
        ossie = convert_starburst_to_ossie(yaml.safe_dump(native))
    orders = yaml.safe_load(ossie)["datasets"][0]
    assert [metric["name"] for metric in orders["metrics"]] == ["total_sales"]
    assert orders["metrics"][0]["expression"]["sql"] == "SUM(net_amount)"
    restored = yaml.safe_load(convert_ossie_to_starburst(ossie))
    assert (
        sorted(restored["semantics"]["metrics"], key=lambda item: item["name"])
        == semantics["metrics"]
    )


@pytest.mark.parametrize(
    ("target", "portable"), [("JSON", False), ("VARCHAR(10)", True)]
)
def test_import_marks_only_supported_cast_targets_portable(target, portable):
    native = yaml.safe_load(FIXTURE.read_text())
    dimension = native["semantics"]["dimensions"][0]
    dimension["sql"] = f"CAST(payload AS {target})"
    ossie = convert_starburst_to_ossie(yaml.safe_dump(native))
    fields = yaml.safe_load(ossie)["datasets"][0]["fields"]
    expression = next(
        field["expression"] for field in fields if field["name"] == "sale_month"
    )
    assert ("sql" in expression) == portable
    assert expression["dialects"] == [
        {"dialect": "TRINO", "expression": dimension["sql"]}
    ]
    restored = yaml.safe_load(convert_ossie_to_starburst(ossie))
    assert restored["semantics"]["dimensions"][0]["sql"] == dimension["sql"]


@pytest.mark.parametrize(
    "sql", ["SUM(net_amount)", "SUM(orders.net_amount + net_amount)"]
)
def test_export_requires_qualified_model_metric_fields(sql):
    model = yaml.safe_load(convert_starburst_to_ossie(FIXTURE.read_text()))
    model.pop("relationships")
    model["datasets"] = model["datasets"][:1]
    model["metrics"] = [{"name": "gross_sales", "expression": {"sql": sql}}]
    with pytest.raises(ConversionError, match="must include a dataset qualifier"):
        convert_ossie_to_starburst(yaml.safe_dump(model))


def test_export_rejects_shadowed_source_qualifier():
    model = yaml.safe_load(convert_starburst_to_ossie(FIXTURE.read_text()))
    model["datasets"][0]["fields"].append(
        {"name": "source", "expression": {"sql": "source"}}
    )
    with pytest.raises(ConversionError, match="field named source shadows"):
        convert_ossie_to_starburst(yaml.safe_dump(model))
