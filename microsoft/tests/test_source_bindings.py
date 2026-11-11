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

import json

import pytest
import yaml

from ossie_microsoft import convert_ossie_to_semantic_model
from ossie_microsoft._common import OSSIE_VERSION, write_stash
from ossie_microsoft.cli import main


@pytest.fixture
def model():
    return {
        "version": OSSIE_VERSION,
        "name": "retail",
        "datasets": [
            {
                "name": dataset,
                "source": f"RETAIL.RAW.{dataset.upper()}",
                "fields": [{"name": "ID", "datatype": "Integer"}],
            }
            for dataset in ("orders", "customers")
        ],
    }


@pytest.fixture
def orders_binding():
    return {
        "server": "retail-prod.snowflakecomputing.com",
        "warehouse": "BI_WH",
        "database": "RETAIL",
        "schema": "REPORTING",
        "table": "SALES_ORDERS",
    }


def _export_args(tmp_path, model, bindings_text):
    input_path = tmp_path / "input.yaml"
    bindings_path = tmp_path / "bindings.yaml"
    output_path = tmp_path / "model.bim"
    input_path.write_text(yaml.safe_dump(model), encoding="utf-8")
    bindings_path.write_text(bindings_text, encoding="utf-8")
    return (
        [
            "export",
            "-i", str(input_path),
            "--source-bindings", str(bindings_path),
            "-o", str(output_path),
        ],
        output_path,
    )


def test_strict_cli_exports_independent_snowflake_partitions(tmp_path, model, orders_binding):
    bindings = {
        "orders": orders_binding,
        "customers": {
            **orders_binding,
            "server": "customer-prod.snowflakecomputing.com",
            "warehouse": "CUSTOMER_WH",
            "table": 'Customers "Current" #(',
            "kind": "view",
            "role": "BI_READER",
        },
    }
    args, output = _export_args(tmp_path, model, yaml.safe_dump(bindings))

    assert main(["--strict", *args]) == 0

    bim = json.loads(output.read_text(encoding="utf-8"))
    assert "expressions" not in bim["model"]
    orders, customers = bim["model"]["tables"]
    assert orders["partitions"] == [{
        "name": "orders",
        "mode": "import",
        "source": {
            "type": "m",
            "expression": [
                "let",
                '    Source = Snowflake.Databases("retail-prod.snowflakecomputing.com", '
                '"BI_WH", [Implementation="2.0"]),',
                '    Database = Source{[Name="RETAIL", Kind="Database"]}[Data],',
                '    Schema = Database{[Name="REPORTING", Kind="Schema"]}[Data],',
                '    Navigation = Schema{[Name="SALES_ORDERS", Kind="Table"]}[Data]',
                "in",
                "    Navigation",
            ],
        },
    }]
    partition = customers["partitions"][0]
    assert partition["mode"] == "import"
    expression = partition["source"]["expression"]
    assert expression[1] == (
        '    Source = Snowflake.Databases("customer-prod.snowflakecomputing.com", '
        '"CUSTOMER_WH", [Implementation="2.0", Role="BI_READER"]),'
    )
    assert expression[4] == (
        '    Navigation = Schema{[Name="Customers ""Current"" #(#)(", Kind="View"]}[Data]'
    )


def test_strict_cli_aborts_before_writing_if_a_binding_is_missing(
    tmp_path, capsys, model, orders_binding
):
    args, output = _export_args(tmp_path, model, yaml.safe_dump({"orders": orders_binding}))
    output.write_text("previous export", encoding="utf-8")

    assert main([*args, "--strict"]) == 1

    assert output.read_text(encoding="utf-8") == "previous export"
    assert "[dataset 'customers'] missing Snowflake source binding" in capsys.readouterr().err


def test_non_strict_cli_warns_and_uses_the_default_for_unbound_datasets(
    tmp_path, capsys, model, orders_binding
):
    args, output = _export_args(tmp_path, model, yaml.safe_dump({"orders": orders_binding}))

    assert main(args) == 0

    orders, customers = json.loads(output.read_text(encoding="utf-8"))["model"]["tables"]
    assert orders["partitions"][0]["mode"] == "import"
    assert customers["partitions"][0]["mode"] == "directLake"
    assert "[dataset 'customers'] missing Snowflake source binding" in capsys.readouterr().err


def test_preserved_partitions_satisfy_strict_binding_and_explicit_bindings_override(
    tmp_path, model, orders_binding
):
    for dataset in model["datasets"]:
        write_stash(dataset, {
            "partitions": [{
                "name": "old",
                "mode": "import",
                "source": {"type": "m", "expression": "#table({}, {})"},
            }],
        })
    args, output = _export_args(tmp_path, model, yaml.safe_dump({"orders": orders_binding}))

    assert main([*args, "--strict"]) == 0

    orders, customers = json.loads(output.read_text(encoding="utf-8"))["model"]["tables"]
    assert orders["partitions"][0]["name"] == "orders"
    assert customers["partitions"][0]["name"] == "old"


def test_rebinding_removes_stale_incremental_refresh_policy(model, orders_binding):
    write_stash(model["datasets"][0], {"refreshPolicy": {"policyType": "basic"}})
    write_stash(model, {"document": {"compatibilityLevel": 1200}})
    model["datasets"].pop()

    with pytest.warns(UserWarning, match="removes any incremental refresh policy"):
        bim = convert_ossie_to_semantic_model(model, source_bindings={"orders": orders_binding})

    assert bim["compatibilityLevel"] == 1400
    assert "refreshPolicy" not in bim["model"]["tables"][0]


@pytest.mark.parametrize(
    ("bindings_text", "error"),
    [
        ("", "mapping keyed by dataset name"),
        ("[]", "mapping keyed by dataset name"),
        ("orders: {}\norders: {}", "unique mapping keys"),
        ("ordres: {}", "unknown dataset 'ordres'"),
        ("orders: []", "must be a mapping"),
        ("orders: {password: ignored}", "unknown fields: password"),
        ("orders: {}", "requires non-empty text for 'database'"),
    ],
)
def test_cli_rejects_invalid_source_bindings(tmp_path, capsys, model, bindings_text, error):
    args, output = _export_args(tmp_path, model, bindings_text)

    assert main(args) == 1

    assert not output.exists()
    assert error in capsys.readouterr().err


def test_cli_rejects_an_unknown_snowflake_object_kind(tmp_path, capsys, model, orders_binding):
    bindings = yaml.safe_dump({"orders": {**orders_binding, "kind": "query"}})
    args, output = _export_args(tmp_path, model, bindings)

    assert main(args) == 1

    assert not output.exists()
    assert "requires kind 'table' or 'view'" in capsys.readouterr().err
