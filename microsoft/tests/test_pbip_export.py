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

"""Export to a Power BI project semantic model folder, with TOM replaced by a fake."""

import json

import pytest
import yaml

from ossie_microsoft import ConversionError, export_powerbi_project
from ossie_microsoft.cli import main
from ossie_microsoft.pbip_export import PBISM
from ossie_microsoft.tom import TomUnavailableError


@pytest.fixture
def written(monkeypatch):
    """Replace TOM's folder serializer with one that records its calls."""
    calls = []

    def fake_serialize_tmdl_folder(document, path, *, assembly_dir=None):
        calls.append((document, path, assembly_dir))
        path.mkdir(parents=True, exist_ok=True)
        (path / "database.tmdl").write_text(f"database {document['name']}\n", encoding="utf-8")

    monkeypatch.setattr(
        "ossie_microsoft.pbip_export.serialize_tmdl_folder", fake_serialize_tmdl_folder
    )
    return calls


def _failing_serializer(monkeypatch, exc):
    def fail(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr("ossie_microsoft.pbip_export.serialize_tmdl_folder", fail)


def test_export_writes_the_semantic_model_folder(osi, tmp_path, written):
    folder = export_powerbi_project(osi, tmp_path / "out", assembly_dir=tmp_path / "tom")

    assert folder == tmp_path / "out" / "sales_model.SemanticModel"
    [(document, path, assembly_dir)] = written
    assert path == folder / "definition"
    assert assembly_dir == tmp_path / "tom"
    assert document["name"] == "sales_model"
    assert {table["name"] for table in document["model"]["tables"]} >= {"Sales", "Customer"}
    pbism = json.loads((folder / "definition.pbism").read_text(encoding="utf-8"))
    assert pbism == PBISM
    assert pbism["version"] == "4.0"


def test_export_accepts_yaml_text(osi, tmp_path, written):
    folder = export_powerbi_project(yaml.safe_dump(osi), tmp_path)
    assert (folder / "definition" / "database.tmdl").is_file()


def test_export_writes_into_an_existing_empty_folder(osi, tmp_path, written):
    (tmp_path / "sales_model.SemanticModel").mkdir()
    folder = export_powerbi_project(osi, tmp_path)
    assert (folder / "definition.pbism").is_file()


def test_export_refuses_a_non_empty_folder_without_force(osi, tmp_path, written):
    folder = export_powerbi_project(osi, tmp_path)
    with pytest.raises(ConversionError, match="already exists and is not empty"):
        export_powerbi_project(osi, tmp_path)
    assert len(written) == 1
    assert folder.is_dir()


def test_force_replaces_the_definition_and_keeps_other_files(osi, tmp_path, written):
    folder = export_powerbi_project(osi, tmp_path)
    (folder / "definition.pbism").write_text("{}", encoding="utf-8")
    (folder / "diagramLayout.json").write_text('{"keep": true}', encoding="utf-8")

    assert export_powerbi_project(osi, tmp_path, force=True) == folder

    assert json.loads((folder / "definition.pbism").read_text(encoding="utf-8")) == PBISM
    assert (folder / "diagramLayout.json").read_text(encoding="utf-8") == '{"keep": true}'
    assert len(written) == 2


def test_export_never_switches_a_tmsl_project_to_tmdl(osi, tmp_path, written):
    folder = tmp_path / "sales_model.SemanticModel"
    folder.mkdir()
    (folder / "model.bim").write_text("{}", encoding="utf-8")
    with pytest.raises(ConversionError, match=r"TMSL \(model.bim\)"):
        export_powerbi_project(osi, tmp_path, force=True)
    assert not written


@pytest.mark.parametrize("name", ["a/b", "a\\b", "sales:2026", "sales?", "  ", "sales."])
def test_export_rejects_a_name_that_cannot_name_a_folder(osi, tmp_path, written, name):
    with pytest.raises(ConversionError, match="cannot name a semantic model folder"):
        export_powerbi_project({**osi, "name": name}, tmp_path)
    assert not written


def test_export_reports_a_managed_tom_failure(osi, tmp_path, monkeypatch):
    error = RuntimeError("fallback")
    error.Message = "column 'Sales'[Amount] is not unique"
    _failing_serializer(monkeypatch, error)
    with pytest.raises(ConversionError, match=r"TOM could not write .*is not unique"):
        export_powerbi_project(osi, tmp_path)
    assert not (tmp_path / "sales_model.SemanticModel" / "definition.pbism").exists()


def test_export_lets_a_missing_tom_setup_through(osi, tmp_path, monkeypatch):
    _failing_serializer(monkeypatch, TomUnavailableError("optional 'tom' extra"))
    with pytest.raises(TomUnavailableError, match="optional 'tom' extra"):
        export_powerbi_project(osi, tmp_path)


def _ossie_file(osi, tmp_path):
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(osi), encoding="utf-8")
    return str(path)


def test_cli_exports_a_project_folder(osi, tmp_path, written, capsys):
    source = _ossie_file(osi, tmp_path)
    out = tmp_path / "project"

    assert main(["export", "-i", source, "--pbip", str(out), "-q"]) == 0

    assert (out / "sales_model.SemanticModel" / "definition.pbism").is_file()
    assert capsys.readouterr().out == ""


def test_cli_needs_force_to_replace_a_project_folder(osi, tmp_path, written, capsys):
    source = _ossie_file(osi, tmp_path)
    out = str(tmp_path / "project")
    assert main(["export", "-i", source, "--pbip", out, "-q"]) == 0

    assert main(["export", "-i", source, "--pbip", out, "-q"]) == 1
    assert "already exists and is not empty" in capsys.readouterr().err
    assert main(["export", "-i", source, "--pbip", out, "--force", "-q"]) == 0


def test_cli_reports_a_missing_tom_setup_without_a_traceback(osi, tmp_path, monkeypatch, capsys):
    _failing_serializer(monkeypatch, TomUnavailableError("optional 'tom' extra"))
    source = _ossie_file(osi, tmp_path)

    assert main(["export", "-i", source, "--pbip", str(tmp_path), "-q"]) == 1
    assert capsys.readouterr().err == "Error: optional 'tom' extra\n"


@pytest.mark.parametrize(
    "extra",
    [["--force"], ["-o", "model.bim", "--pbip", "out"]],
    ids=["force-without-pbip", "output-and-pbip"],
)
def test_cli_rejects_conflicting_export_targets(osi, tmp_path, capsys, extra):
    source = _ossie_file(osi, tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        main(["export", "-i", source, *extra])
    assert exit_info.value.code == 2
