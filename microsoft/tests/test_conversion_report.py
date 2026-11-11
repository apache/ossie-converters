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
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json

import pytest
import yaml

from ossie_microsoft._common import OSSIE_VERSION, make_expression, read_stash
from ossie_microsoft.cli import main


def test_export_reports_native_objects_placeholders_and_losses(tmp_path, capsys):
    ossie = {
        "name": "retail",
        "version": OSSIE_VERSION,
        "datasets": [{
            "name": "orders",
            "source": "RETAIL.REPORTING.ORDERS",
            "fields": [
                {"name": "amount", "datatype": "Decimal", "label": "Revenue"},
                {"name": "rounded", "expression": make_expression("ROUND(amount)", "SNOWFLAKE")},
                {},
            ],
            "metrics": [{"name": "per_order"}],
            "custom_extensions": [
                {"vendor_name": "POWER_BI", "data": "{}"},
                {
                    "vendor": "SNOWFLAKE",
                    "content": '{"metrics":[{"name":"local_revenue","expr":"SUM(amount)"}]}',
                },
                {"vendor_name": "OTHER", "data": "not JSON"},
            ],
        }],
        "metrics": [
            {"name": "revenue", "expression": make_expression("SUM(orders.amount)", "SNOWFLAKE")},
            {"name": "intentional_blank", "expression": make_expression("BLANK()", "DAX")},
            {"name": "quantile", "expression": make_expression("PERCENTILE(amount)", "SNOWFLAKE")},
            {"name": "missing"},
            {},
        ],
        "relationships": [
            {
                "name": "single",
                "from": "orders", "from_columns": ["amount"],
                "to": "orders", "to_columns": ["amount"],
            },
            {
                "name": "composite",
                "from": "orders", "from_columns": ["amount", "rounded"],
                "to": "orders", "to_columns": ["amount", "rounded"],
            },
            {
                "name": "unresolved",
                "from": "missing", "from_columns": ["amount"],
                "to": "orders", "to_columns": ["amount"],
            },
            None,
        ],
    }
    source = tmp_path / "model.yaml"
    output = tmp_path / "model.bim"
    report_path = tmp_path / "report.json"
    source.write_text(yaml.safe_dump(ossie), encoding="utf-8")
    args = ["export", "-i", str(source), "-o", str(output), "--quiet"]

    assert main(args) == 0
    original = output.read_bytes()
    assert main(["--report", str(report_path), *args, "--strict"]) == 1
    assert output.read_bytes() == original
    assert capsys.readouterr().err == ""

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["version"] == 1
    assert report["direction"] == "export"
    assert report["completed"] is True
    assert report["warnings"]
    entries = {
        entry["source_path"]: entry for entry in report["entries"] if "payload_path" not in entry
    }
    assert {path: entry["status"] for path, entry in entries.items()} == {
        "/datasets/0/custom_extensions/1": "dropped",
        "/datasets/0/custom_extensions/2": "dropped",
        "/datasets/0/metrics/0": "dropped",
        "/datasets/0/fields/0": "translated",
        "/datasets/0/fields/1": "placeholder",
        "/datasets/0/fields/2": "dropped",
        "/metrics/0": "translated",
        "/metrics/1": "translated",
        "/metrics/2": "placeholder",
        "/metrics/3": "dropped",
        "/metrics/4": "dropped",
        "/relationships/0": "translated",
        "/relationships/1": "dropped",
        "/relationships/2": "dropped",
        "/relationships/3": "dropped",
    }
    assert entries["/datasets/0/fields/0"]["dropped_properties"] == ["label"]
    assert entries["/datasets/0/fields/1"]["metadata_properties"] == [
        "OssieExpressionDialect", "OssieExpression",
    ]
    assert entries["/metrics/2"]["target_dataset"] == "orders"
    assert entries["/metrics/3"]["reason"] == "missing_expression"
    assert entries["/datasets/0/custom_extensions/1"]["vendor"] == "SNOWFLAKE"
    assert entries["/datasets/0/custom_extensions/1"]["properties"] == ["metrics"]
    assert entries["/datasets/0/custom_extensions/2"]["vendor"] == "OTHER"
    assert [entry for entry in report["entries"] if "payload_path" in entry] == [{
        "kind": "metric",
        "source_path": "/datasets/0/custom_extensions/1",
        "payload_path": "/metrics/0",
        "name": "local_revenue",
        "status": "dropped",
        "vendor": "SNOWFLAKE",
        "reason": "unsupported_vendor",
    }]
    assert "SUM(amount)" not in report_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("project", [False, True])
def test_import_reports_vendor_metadata_and_original_source_paths(tmp_path, project):
    relationship = {
        "name": "active",
        "fromTable": "Orders", "fromColumn": "ID",
        "toTable": "Orders", "toColumn": "ID",
    }
    bim = {
        "name": "retail",
        "model": {
            "tables": [
                {
                    "name": "Private",
                    "isPrivate": True,
                    "columns": [{"name": "ID"}],
                    "measures": [{"name": "Internal", "expression": "42"}],
                },
                {
                    "name": "Orders",
                    "columns": [
                        {"name": "ID", "dataType": "int64", "isHidden": True},
                        {"name": "RowNumber", "type": "rowNumber"},
                        {},
                    ],
                    "measures": [{"name": "Count", "expression": "COUNTROWS('Orders')"},
                                 {"name": "Missing"}, {}],
                },
            ],
            "relationships": [relationship, {**relationship, "name": "inactive", "isActive": False},
                              None],
        },
    }
    directory = tmp_path / "Retail.SemanticModel"
    directory.mkdir()
    source = directory / "model.bim"
    source.write_text(json.dumps(bim), encoding="utf-8")
    output = tmp_path / "model.yaml"
    report_path = tmp_path / "report.json"
    args = [
        "import", "-i", str(directory if project else source), "-o", str(output),
        "--report", str(report_path), "--quiet",
    ]
    assert main(args) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["direction"] == "import"
    assert report["completed"] is True
    entries = {entry["source_path"]: entry for entry in report["entries"]}
    assert {path: entry["status"] for path, entry in entries.items()} == {
        "/model/tables/0/columns/0": "vendor_metadata",
        "/model/tables/0/measures/0": "vendor_metadata",
        "/model/tables/1/columns/0": "translated",
        "/model/tables/1/columns/1": "vendor_metadata",
        "/model/tables/1/columns/2": "dropped",
        "/model/tables/1/measures/0": "translated",
        "/model/tables/1/measures/1": "vendor_metadata",
        "/model/tables/1/measures/2": "dropped",
        "/model/relationships/0": "translated",
        "/model/relationships/1": "vendor_metadata",
        "/model/relationships/2": "dropped",
    }
    assert entries["/model/tables/1/columns/0"]["metadata_properties"] == ["isHidden"]
    assert entries["/model/tables/1/measures/1"]["reason"] == "missing_expression"
    assert entries["/model/relationships/1"]["vendor"] == "POWER_BI"
    assert entries["/model/relationships/0"]["target_name"] == "Orders_ID_to_Orders_ID"
    stash = read_stash(yaml.safe_load(output.read_text(encoding="utf-8")))
    assert stash["excludedRelationships"][0]["name"] == "inactive"
    assert stash["excludedMeasures"][0]["measure"]["name"] == "Missing"


def test_report_records_a_conversion_error(tmp_path, capsys):
    source = tmp_path / "model.yaml"
    source.write_text("version: 0.2.0.dev0\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    output = tmp_path / "model.bim"

    assert main(["export", "-i", str(source), "-o", str(output),
                 "--report", str(report_path)]) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["completed"] is False
    assert "requires 'name' and 'datasets'" in report["error"]
    assert report["entries"] == []
    assert not output.exists()
    assert "Error:" in capsys.readouterr().err


@pytest.mark.parametrize("collision", ["input", "output", "hardlink"])
def test_report_refuses_to_overwrite_the_input_or_output(tmp_path, capsys, collision):
    source = tmp_path / "model.yaml"
    source.write_text("input", encoding="utf-8")
    output = tmp_path / "model.bim"
    if collision == "hardlink":
        report = tmp_path / "report.json"
        report.hardlink_to(source)
    else:
        report = source if collision == "input" else output

    assert main(["export", "-i", str(source), "-o", str(output), "--report", str(report)]) == 1

    assert "--report must be separate" in capsys.readouterr().err
    assert source.read_text(encoding="utf-8") == "input"
    assert not output.exists()


def test_report_file_write_error_is_reported(tmp_path, capsys):
    source = tmp_path / "model.yaml"
    report_path = tmp_path / "missing_parent" / "report.json"
    assert main(["export", "-i", str(source), "--report", str(report_path)]) == 1
    assert "Error writing conversion report:" in capsys.readouterr().err
