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
from pathlib import Path

import pytest
import yaml

from ossie_microsoft._common import OSSIE_VERSION, make_expression, read_stash, write_stash
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
            "nonportable_extensions": [
                {"vendor_name": "POWER_BI", "data": {}},
                {
                    "vendor_name": "SNOWFLAKE",
                    "data": {"metrics": [{"name": "local_revenue", "expr": "SUM(amount)"}]},
                },
                {"vendor_name": "OTHER", "data": {}},
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
        "": "placeholder",
        "/datasets/0": "translated",
        "/datasets/0/nonportable_extensions/1": "dropped",
        "/datasets/0/nonportable_extensions/2": "dropped",
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
    assert entries["/datasets/0/nonportable_extensions/1"]["vendor"] == "SNOWFLAKE"
    assert entries["/datasets/0/nonportable_extensions/1"]["properties"] == ["metrics"]
    assert entries["/datasets/0/nonportable_extensions/2"]["vendor"] == "OTHER"
    assert [entry for entry in report["entries"] if "payload_path" in entry] == [{
        "kind": "metric",
        "source_path": "/datasets/0/nonportable_extensions/1",
        "payload_path": "/metrics/0",
        "name": "local_revenue",
        "status": "dropped",
        "vendor": "SNOWFLAKE",
        "reason": "unsupported_vendor",
    }]
    assert "SUM(amount)" not in report_path.read_text(encoding="utf-8")


def test_export_preserves_native_extensions(tmp_path, capsys):
    ossie = {
        "name": "retail",
        "version": OSSIE_VERSION,
        "datasets": [{"name": "orders", "fields": [{"name": "id"}]}],
        "nonportable_extensions": [
            {"vendor_name": "POWER_BI", "data": {"culture": "fr-FR"}},
            {"vendor_name": "SNOWFLAKE", "data": {}},
        ],
    }
    source = tmp_path / "model.yaml"
    source.write_text(yaml.safe_dump(ossie), encoding="utf-8")
    report_path = tmp_path / "report.json"

    assert main(["export", "-i", str(source), "--report", str(report_path)]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["model"]["culture"] == "fr-FR"
    assert "vendor 'POWER_BI'" not in captured.err
    assert "vendor 'SNOWFLAKE'" in captured.err
    report = json.loads(report_path.read_text(encoding="utf-8"))
    extensions = [entry for entry in report["entries"] if entry["kind"] == "extension"]
    assert [(entry["source_path"], entry["vendor"]) for entry in extensions] == [
        ("/nonportable_extensions/1", "SNOWFLAKE"),
    ]


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
        "/model": "translated",
        "/model/tables/0": "vendor_metadata",
        "/model/tables/1": "translated",
        "/model/tables/0/columns/0": "vendor_metadata",
        "/model/tables/0/measures/0": "vendor_metadata",
        "/model/tables/1/columns/0": "translated",
        "/model/tables/1/columns/1": "vendor_metadata",
        "/model/tables/1/columns/2": "dropped",
        "/model/tables/1/measures/0": "source_dialect",
        "/model/tables/1/measures/1": "vendor_metadata",
        "/model/tables/1/measures/2": "dropped",
        "/model/relationships/0": "translated",
        "/model/relationships/1": "vendor_metadata",
        "/model/relationships/2": "dropped",
    }
    assert entries["/model/tables/0"]["table"] == "Private"
    assert entries["/model/tables/0"]["detail"]
    assert entries["/model/tables/0/columns/0"]["table"] == "Private"
    assert entries["/model/tables/0/measures/0"]["table"] == "Private"
    assert entries["/model/tables/1/columns/0"]["metadata_properties"] == ["isHidden"]
    assert entries["/model/tables/1/measures/1"]["reason"] == "missing_expression"
    assert entries["/model/relationships/1"]["vendor"] == "POWER_BI"
    assert entries["/model/relationships/0"]["target_name"] == "Orders_ID_to_Orders_ID"
    stash = read_stash(yaml.safe_load(output.read_text(encoding="utf-8")))
    assert stash["excludedRelationships"][0]["name"] == "inactive"
    assert stash["excludedMeasures"][0]["measure"]["name"] == "Missing"


def test_report_records_a_conversion_error(tmp_path, capsys):
    source = tmp_path / "model.yaml"
    source.write_text(f"version: {OSSIE_VERSION}\n", encoding="utf-8")
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
    errors = capsys.readouterr().err
    assert "Error writing conversion report:" in errors
    assert "Error: [Errno 2]" in errors
    assert str(source) in errors


@pytest.mark.parametrize("quiet", [False, True])
def test_strict_export_reports_and_warns_for_every_dropped_dataset_metric(tmp_path, capsys, quiet):
    model = {"name": "shop", "version": OSSIE_VERSION, "datasets": [{
        "name": "Sales", "source": "Sales", "fields": [{"name": "id", "datatype": "Integer"}],
        "metrics": [{"name": name, "expression": make_expression("COUNTROWS(Sales)", "DAX")}
                    for name in ("line_count", "other_count")],
    }]}
    write_stash(model["datasets"][0], {"partitions": [{"name": "Sales", "mode": "import",
                "source": {"type": "m", "expression": "#table({\"id\"}, {{1}})"}}]})
    source, report = tmp_path / "source.yaml", tmp_path / "report.json"
    source.write_text(yaml.safe_dump(model), encoding="utf-8")
    args = ["export", "-i", str(source), "--strict", "--report", str(report)]
    if quiet:
        args.append("--quiet")
    assert main(args) == 1
    recorded = json.loads(report.read_text())
    assert recorded["completed"] is True
    assert len(recorded["warnings"]) == 2
    for name in ("line_count", "other_count"):
        assert any(name in warning for warning in recorded["warnings"])
        assert any(e.get("name") == name and e["status"] == "dropped"
                   for e in recorded["entries"])
    stderr = capsys.readouterr().err
    assert ("dataset-scoped metric" not in stderr) if quiet else ("dataset-scoped metric" in stderr)


@pytest.mark.parametrize("direction", ["import", "export"])
def test_output_write_failure_is_a_controlled_error_in_the_report(tmp_path, capsys, direction):
    if direction == "import":
        source = Path(__file__).parent / "fixtures" / "sales_model.bim"
    else:
        source = tmp_path / "source.yaml"
        source.write_text(yaml.safe_dump({"version": OSSIE_VERSION, "name": "m", "datasets": [
            {"name": "Sales", "source": "Sales", "fields": [{"name": "id", "datatype": "Integer"}]}
        ]}))
    report = tmp_path / "report.json"
    output = tmp_path / "missing" / "model"
    assert main([direction, "-i", str(source), "-o", str(output), "--report", str(report)]) == 1
    recorded = json.loads(report.read_text())
    assert recorded["completed"] is False
    assert str(output) in recorded["error"]
    assert recorded["entries"]
    stderr = capsys.readouterr().err
    assert "Error:" in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize("direction", ["import", "export"])
@pytest.mark.parametrize("failure", [AttributeError("unexpected failure"), KeyboardInterrupt()])
def test_aborted_run_replaces_an_existing_success_report(tmp_path, monkeypatch, direction, failure):
    from ossie_microsoft import cli
    from ossie_microsoft._report import record

    source = Path(__file__).parent / "fixtures" / "sales_model.bim"
    report = tmp_path / "report.json"
    assert main(["import", "-i", str(source), "--report", str(report), "--quiet"]) == 0
    assert json.loads(report.read_text())["completed"] is True

    def abort(args):
        record("field", "/partial", {"name": "partial"}, "translated")
        raise failure

    monkeypatch.setattr(cli, "_run_" + direction, abort)
    args = [direction, "-i", str(source), "--report", str(report), "--quiet"]
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            main(args)
    else:
        assert main(args) == 1
    recorded = json.loads(report.read_text())
    assert recorded["completed"] is False
    assert recorded["error"] == (str(failure) or type(failure).__name__)
    assert [e["name"] for e in recorded["entries"]] == ["partial"]
    monkeypatch.undo()
    assert main(["import", "-i", str(source), "--report", str(report), "--quiet"]) == 0
    recovered = json.loads(report.read_text())
    assert recovered["completed"] is True
    assert "error" not in recovered
    assert not any(e.get("name") == "partial" for e in recovered["entries"])


def test_invalid_expression_does_not_leave_a_stale_success_report(tmp_path):
    source = tmp_path / "source.yaml"
    report = tmp_path / "report.json"
    model = {"name": "shop", "datasets": [{"name": "Sales", "fields": [{"name": "id"}]}]}
    source.write_text(yaml.safe_dump(model))
    args = ["export", "-i", str(source), "--report", str(report), "--quiet"]
    assert main(args) == 0
    model["datasets"][0]["fields"][0]["expression"] = 42
    source.write_text(yaml.safe_dump(model))
    assert main(args) == 1
    recorded = json.loads(report.read_text())
    assert recorded["completed"] is False
    assert "error" in recorded


@pytest.mark.parametrize(
    ("translate", "expression", "status", "reason"),
    [
        (False, "SUM(Orders[Amount])", "source_dialect", "dax_translation_disabled"),
        (True, "SUM(Orders[Amount])", "translated", None),
        (True, "UNSUPPORTED(Orders[Amount])", "source_dialect", "dax_translation_refused"),
    ],
)
def test_import_report_distinguishes_dax_outcomes(tmp_path, translate, expression, status, reason):
    source, report, output = (tmp_path / name for name in ("source.bim", "report.json", "out.yaml"))
    source.write_text(json.dumps({"name": "shop", "model": {"tables": [{
        "name": "Orders", "columns": [{"name": "Amount", "dataType": "decimal"}],
        "measures": [{"name": "Revenue", "expression": expression}],
    }]}}))
    args = ["import", "-i", str(source), "-o", str(output), "--report", str(report)]
    if translate:
        args.append("--translate-dax")
    assert main(args) == 0
    recorded = json.loads(report.read_text())
    entry = next(e for e in recorded["entries"] if e["kind"] == "metric")
    assert entry["status"] == status
    assert entry.get("reason") == reason
    assert entry["table"] == "Orders"
    dialects = yaml.safe_load(output.read_text())["metrics"][0]["expression"]["dialects"]
    assert {e["dialect"] for e in dialects} == (
        {"DAX", "OSSIE_SQL_2026"} if status == "translated" else {"DAX"}
    )
    if reason == "dax_translation_refused":
        assert entry["detail"]
        assert any(entry["detail"] in warning for warning in recorded["warnings"])
    else:
        assert "detail" not in entry


def test_export_report_places_model_dataset_key_and_source_losses(tmp_path):
    source, report, output = (tmp_path / name for name in ("source.yaml", "report.json", "out.bim"))
    source.write_text(yaml.safe_dump({
        "name": "shop", "label": "Shop", "datasets": [{
            "name": "Orders", "label": "Orders label", "source": "Orders",
            "primary_key": ["id", "region"], "unique_keys": [["id", "region"]],
            "fields": [{"name": "id"}, {"name": "region"}],
        }],
    }))
    assert main(["export", "-i", str(source), "-o", str(output), "--report", str(report)]) == 0
    entries = json.loads(report.read_text())["entries"]
    by_kind = {e["kind"]: e for e in entries}
    assert by_kind["model"]["dropped_properties"] == ["label"]
    assert by_kind["dataset"]["dropped_properties"] == ["label"]
    keys = [e for e in entries if e["kind"] == "key"]
    assert {e["source_path"] for e in keys} == {
        "/datasets/0/primary_key", "/datasets/0/unique_keys/0",
    }
    assert all(e["status"] == "dropped" and e["columns"] == ["id", "region"] for e in keys)
    assert by_kind["source"]["reason"] == "missing_source_ids"
    assert by_kind["source"]["missing_ids"] == ["workspaceId", "itemId"]
    assert by_kind["source"]["source_path"] == ""
    table = json.loads(output.read_text())["model"]["tables"][0]
    assert not any(c.get("isKey") or c.get("isUnique") for c in table["columns"])


def test_import_report_records_preserved_model_and_dataset_properties(tmp_path):
    source, report = (tmp_path / name for name in ("source.bim", "report.json"))
    source.write_text(json.dumps({"name": "shop", "model": {
        "roles": [], "tables": [{"name": "Orders", "hierarchies": [],
                                  "columns": [{"name": "id"}]}],
    }}))
    assert main(["import", "-i", str(source), "--report", str(report), "--quiet"]) == 0
    entries = {e["kind"]: e for e in json.loads(report.read_text())["entries"]}
    assert entries["model"]["metadata_properties"] == ["roles"]
    assert entries["dataset"]["metadata_properties"] == ["hierarchies"]


def test_success_report_does_not_capture_an_outer_exception(tmp_path):
    source = Path(__file__).parent / "fixtures" / "sales_model.bim"
    report = tmp_path / "report.json"
    try:
        raise RuntimeError("unrelated caller failure")
    except RuntimeError:
        assert main(["import", "-i", str(source), "--report", str(report), "--quiet"]) == 0
    recorded = json.loads(report.read_text())
    assert recorded["completed"] is True
    assert "error" not in recorded


@pytest.mark.parametrize("fails", [False, True])
def test_pbip_export_finalizes_report_without_writing_stdout(tmp_path, monkeypatch, capsys, fails):
    from ossie_microsoft import cli
    from ossie_microsoft._report import record
    from ossie_microsoft.tom import TomUnavailableError

    source, report = tmp_path / "source.yaml", tmp_path / "report.json"
    source.write_text("name: shop")
    target = tmp_path / "project"

    def export_project(
        text, destination, force=False, source_bindings=None, require_source_bindings=False
    ):
        assert source_bindings is None
        assert require_source_bindings is False
        assert destination == str(target)
        record("dataset", "/datasets/0", {"name": "Orders"}, "translated")
        if fails:
            raise TomUnavailableError("TOM unavailable")
        return target / "shop.SemanticModel"

    monkeypatch.setattr(cli, "export_powerbi_project", export_project)
    assert main(["export", "-i", str(source), "--pbip", str(target),
                 "--report", str(report)]) == int(fails)
    recorded = json.loads(report.read_text())
    assert recorded["completed"] is (not fails)
    assert recorded["entries"][0]["name"] == "Orders"
    assert recorded.get("error") == ("TOM unavailable" if fails else None)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("translate", [False, True])
def test_calculated_column_translation_keeps_report_paths_and_metric_outcomes(tmp_path, translate):
    source, report, output = (tmp_path / name for name in ("source.bim", "report.json", "out.yaml"))
    source.write_text(json.dumps({"name": "shop", "model": {"tables": [{
        "name": "Orders", "columns": [
            {"name": "Amount", "sourceColumn": "amount", "dataType": "double"},
            {"name": "Gross", "type": "calculated", "dataType": "double",
             "expression": "Orders[Amount] * 1.2"},
        ], "measures": [{"name": "Revenue", "expression": "SUM(Orders[Amount])"}],
    }]}}))
    args = ["import", "-i", str(source), "-o", str(output), "--report", str(report)]
    if translate:
        args.append("--translate-dax")
    assert main(args) == 0
    entries = {e["source_path"]: e for e in json.loads(report.read_text())["entries"]}
    assert entries["/model/tables/0/columns/1"]["name"] == "Gross"
    assert entries["/model/tables/0/columns/1"]["table"] == "Orders"
    assert entries["/model/tables/0/measures/0"]["status"] == (
        "translated" if translate else "source_dialect"
    )
    column = yaml.safe_load(output.read_text())["datasets"][0]["fields"][1]
    dialects = {d["dialect"]: d["expression"] for d in column["expression"]["dialects"]}
    assert dialects["DAX"] == "Orders[Amount] * 1.2"
    if translate:
        assert dialects["OSSIE_SQL_2026"] == "amount * 1.2"
    else:
        assert set(dialects) == {"DAX"}


@pytest.mark.parametrize(
    ("translate", "expression", "nullable", "status", "reason"),
    [
        (False, "UPPER([First])", False, "source_dialect", "dax_translation_disabled"),
        (True, "UPPER([First])", False, "translated", None),
        (True, "TRIM([First])", False, "source_dialect", "dax_translation_refused"),
        (True, "UPPER([First])", True, "source_dialect", "dax_translation_refused"),
    ],
)
def test_calculated_column_report_distinguishes_dax_outcomes(
    tmp_path, translate, expression, nullable, status, reason
):
    source, report, output = (tmp_path / name for name in ("source.bim", "report.json", "out.yaml"))
    source.write_text(json.dumps({"name": "shop", "model": {"tables": [{
        "name": "People", "columns": [
            {"name": "First", "sourceColumn": "first_name", "dataType": "string",
             "isNullable": nullable},
            {"name": "Display", "type": "calculated", "dataType": "string",
             "expression": expression},
        ],
    }]}}))
    args = ["import", "-i", str(source), "-o", str(output), "--report", str(report)]
    if translate:
        args.append("--translate-dax")
    assert main(args) == 0
    recorded = json.loads(report.read_text())
    entries = {e["source_path"]: e for e in recorded["entries"]}
    entry = entries["/model/tables/0/columns/1"]
    assert entry["status"] == status
    assert entry.get("reason") == reason
    assert entry["source_dialect"] == "DAX"
    assert entry["target_dialect"] == ("OSSIE_SQL_2026" if status == "translated" else "DAX")
    assert entry["table"] == "People"
    field = yaml.safe_load(output.read_text())["datasets"][0]["fields"][1]
    dialects = field["expression"]["dialects"]
    assert {d["dialect"] for d in dialects} == (
        {"DAX", "OSSIE_SQL_2026"} if status == "translated" else {"DAX"}
    )
    assert next(d["expression"] for d in dialects if d["dialect"] == "DAX") == expression
    if reason == "dax_translation_refused":
        assert entry["detail"]
        assert any(entry["detail"] in warning for warning in recorded["warnings"])
    else:
        assert "detail" not in entry
    assert entries["/model/tables/0/columns/0"]["status"] == "translated"
    assert "source_dialect" not in entries["/model/tables/0/columns/0"]


@pytest.mark.parametrize(
    "artifact,alias",
    [
        ("bim", "direct"),
        ("bim", "symlink"),
        ("bim", "hardlink"),
        ("tmdl", "direct"),
        ("pbir", "direct"),
    ],
)
def test_report_cannot_overwrite_project_inputs(tmp_path, monkeypatch, capsys, artifact, alias):
    bim = {"name": "shop", "model": {"tables": [{"name": "Orders", "columns": [{"name": "id"}]}]}}
    folder = tmp_path / "Shop.SemanticModel"
    folder.mkdir()
    if artifact == "tmdl":
        source = folder / "definition"
        source.mkdir()
        protected = source / "database.tmdl"
        protected.write_text("database shop\n")
        monkeypatch.setattr("ossie_microsoft.pbip.deserialize_tmdl_documents", lambda _: bim)
    else:
        protected = folder / "model.bim"
        protected.write_text(json.dumps(bim))
        source = folder
        if artifact == "pbir":
            report_folder = tmp_path / "Shop.Report"
            report_folder.mkdir()
            protected = report_folder / "definition.pbir"
            protected.write_text(
                json.dumps({"datasetReference": {"byPath": {"path": "../Shop.SemanticModel"}}})
            )
            source = tmp_path / "Shop.pbip"
            source.write_text(json.dumps({"artifacts": [{"report": {"path": "Shop.Report"}}]}))
    before = protected.read_bytes()
    report = protected
    if alias != "direct":
        report = tmp_path / "report.json"
        if alias == "symlink":
            report.symlink_to(protected)
        else:
            report.hardlink_to(protected)
    output = tmp_path / "out.yaml"
    assert main(["import", "-i", str(source), "-o", str(output), "--report", str(report)]) == 1
    assert "--report must be separate" in capsys.readouterr().err
    assert protected.read_bytes() == before
    assert not output.exists()


def test_report_cannot_overwrite_source_bindings(tmp_path, capsys):
    source, bindings, output = (
        tmp_path / name for name in ("model.yaml", "bindings.yaml", "out.bim")
    )
    source.write_text(
        yaml.safe_dump(
            {"name": "shop", "datasets": [{"name": "Orders", "fields": [{"name": "id"}]}]}
        )
    )
    bindings.write_text(
        yaml.safe_dump(
            {
                "Orders": {
                    "server": "host",
                    "warehouse": "WH",
                    "database": "DB",
                    "schema": "PUBLIC",
                    "table": "ORDERS",
                }
            }
        )
    )
    before = bindings.read_bytes()
    assert (
        main(
            [
                "export",
                "-i",
                str(source),
                "-o",
                str(output),
                "--source-bindings",
                str(bindings),
                "--report",
                str(bindings),
            ]
        )
        == 1
    )
    assert "--report must be separate" in capsys.readouterr().err
    assert bindings.read_bytes() == before
    assert not output.exists()


@pytest.mark.parametrize("artifact", ["metadata", "tmdl", "hardlink"])
def test_report_cannot_overwrite_pbip_outputs(tmp_path, monkeypatch, capsys, artifact):
    source = tmp_path / "model.yaml"
    source.write_text(
        yaml.safe_dump(
            {"name": "shop", "datasets": [{"name": "Orders", "fields": [{"name": "id"}]}]}
        )
    )
    target = tmp_path / "Shop.SemanticModel"
    report = (
        target / "definition.pbism" if artifact != "tmdl" else target / "definition/database.tmdl"
    )
    protected = None
    if artifact == "hardlink":
        target.mkdir()
        protected = target / "definition.pbism"
        protected.write_text("existing model metadata")
        report = tmp_path / "report.json"
        report.hardlink_to(protected)

    def serialize(document, path, *, assembly_dir=None):
        path.mkdir(parents=True, exist_ok=True)
        (path / "database.tmdl").write_text("database shop\n")

    monkeypatch.setattr("ossie_microsoft.pbip_export.serialize_tmdl_folder", serialize)
    assert (
        main(
            ["export", "-i", str(source), "--pbip", str(target), "--force", "--report", str(report)]
        )
        == 1
    )
    assert "--report must" in capsys.readouterr().err
    if protected:
        assert protected.read_text() == "existing model metadata"
        assert not (target / "definition").exists()
    else:
        assert not target.exists()


def test_export_report_pointers_resolve_in_legacy_and_flat_source_documents(tmp_path):
    for wrapped in (True, False):
        extension_key = "custom_extensions" if wrapped else "nonportable_extensions"
        foreign = [{"vendor_name": "SNOWFLAKE", "data": {}}]
        model = {
            "name": "shop",
            "datasets": [
                {
                    "name": "Orders",
                    "fields": [
                        {"name": "id", extension_key: foreign},
                        {"name": "region"},
                    ],
                    "primary_key": ["id", "region"],
                    "unique_keys": [["id", "region"]],
                }
            ],
            "metrics": [
                {"name": "Rows", "expression": make_expression("COUNTROWS(Orders)", "DAX")}
            ],
            "relationships": [
                {
                    "name": "self",
                    "from": "Orders",
                    "to": "Orders",
                    "from_columns": ["id"],
                    "to_columns": ["id"],
                }
            ],
            extension_key: foreign,
        }
        document = (
            {"version": "0.1.1", "semantic_model": [model]}
            if wrapped
            else {"version": OSSIE_VERSION, **model}
        )
        source, report = tmp_path / "source.yaml", tmp_path / "report.json"
        source.write_text(yaml.safe_dump(document))
        assert main(["export", "-i", str(source), "--report", str(report), "--quiet"]) == 0
        entries = json.loads(report.read_text())["entries"]
        prefix = "/semantic_model/0" if wrapped else ""
        paths = {e["source_path"] for e in entries}
        assert {
            prefix,
            f"{prefix}/datasets/0",
            f"{prefix}/datasets/0/fields/0",
            f"{prefix}/datasets/0/primary_key",
            f"{prefix}/datasets/0/unique_keys/0",
            f"{prefix}/metrics/0",
            f"{prefix}/relationships/0",
            f"{prefix}/{extension_key}/0",
            f"{prefix}/datasets/0/fields/0/{extension_key}/0",
        } <= paths
        for path in paths:
            value = document
            for part in path.split("/")[1:]:
                key = part.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
