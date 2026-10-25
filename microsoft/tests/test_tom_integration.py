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

import copy
import json
import os
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pythonnet", reason="optional TOM extra is not installed")
pytest.importorskip("clr_loader", reason="optional TOM extra is not installed")

from ossie_microsoft import (  # noqa: E402
    convert_ossie_to_semantic_model,
    convert_semantic_model_to_ossie,
    export_powerbi_project,
    validate_bim,
    validate_tmsl,
)

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "sales_model.bim"
ASSEMBLIES = Path(
    os.environ.get(
        "OSSIE_MICROSOFT_TOM_ASSEMBLIES",
        Path.cwd() / ".tom" / "assemblies",
    )
)
if not (ASSEMBLIES / "Microsoft.AnalysisServices.Tabular.dll").is_file():
    pytest.skip("optional TOM assemblies have not been restored", allow_module_level=True)


def test_sales_fixture_and_round_trip_are_tom_clean():
    original = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    assert validate_tmsl(original, assembly_dir=ASSEMBLIES).is_valid

    ossie = convert_semantic_model_to_ossie(original)
    exported = convert_ossie_to_semantic_model(ossie)
    assert validate_tmsl(exported, assembly_dir=ASSEMBLIES).is_valid


def test_tpcds_export_is_tom_clean():
    example = HERE.parents[1] / "ossie" / "examples" / "tpcds_semantic_model.yaml"
    exported = convert_ossie_to_semantic_model(example.read_text(encoding="utf-8"))
    assert validate_tmsl(exported, assembly_dir=ASSEMBLIES).is_valid


def test_tmdl_export_is_a_single_document(monkeypatch):
    example = HERE.parents[1] / "ossie" / "examples" / "tpcds_semantic_model.yaml"
    monkeypatch.setenv("OSSIE_MICROSOFT_TOM_ASSEMBLIES", os.fspath(ASSEMBLIES))

    tmdl = convert_ossie_to_semantic_model(
        example.read_text(encoding="utf-8"), output_format="TMDL"
    )

    assert isinstance(tmdl, str)
    assert tmdl.startswith("database ")
    assert "model Model" in tmdl
    assert "table store_sales" in tmdl


def test_a_tmdl_document_imports_back_to_an_equivalent_ossie_model(monkeypatch):
    monkeypatch.setenv("OSSIE_MICROSOFT_TOM_ASSEMBLIES", os.fspath(ASSEMBLIES))
    original = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))

    from_tmsl = yaml.safe_load(convert_semantic_model_to_ossie(original))
    tmdl = convert_ossie_to_semantic_model(
        yaml.safe_dump(from_tmsl), output_format="TMDL"
    )
    from_tmdl = yaml.safe_load(convert_semantic_model_to_ossie(tmdl))

    expected = from_tmsl
    received = from_tmdl
    assert received["name"] == expected["name"]
    assert received["description"] == expected["description"]
    assert [d["name"] for d in received["datasets"]] == [
        d["name"] for d in expected["datasets"]
    ]
    assert [m["name"] for m in received["metrics"]] == [
        m["name"] for m in expected["metrics"]
    ]
    assert [r["name"] for r in received["relationships"]] == [
        r["name"] for r in expected["relationships"]
    ]


def test_tom_validation_catches_a_dangling_hierarchy_reference(tmp_path):
    model = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    broken = copy.deepcopy(model)
    local_date_table = next(
        table
        for table in broken["model"]["tables"]
        if table["name"] == "LocalDateTable_9f2a1b3c"
    )
    local_date_table.pop("hierarchies")
    path = tmp_path / "broken.bim"
    path.write_text(json.dumps(broken), encoding="utf-8")

    result = validate_bim(path, assembly_dir=ASSEMBLIES)
    assert not result.is_valid
    assert any("DefaultHierarchy" in error.message for error in result.errors)
    with pytest.raises(ValueError, match="DefaultHierarchy"):
        result.raise_for_errors()


def _read_definition_folder(path):
    """Load a TMDL folder with TOM itself, independently of the converter's own reader."""
    from ossie_microsoft.tom import _load_tom

    tom = _load_tom(ASSEMBLIES.resolve())
    database = tom.TmdlSerializer.DeserializeDatabaseFromFolder(os.fspath(path))
    return json.loads(tom.JsonSerializer.SerializeDatabase(database))


def test_pbip_export_writes_the_layout_power_bi_reads(tmp_path):
    original = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    ossie = convert_semantic_model_to_ossie(original)

    folder = export_powerbi_project(ossie, tmp_path, assembly_dir=ASSEMBLIES)

    definition = folder / "definition"
    assert folder.name == "sales_model.SemanticModel"
    assert json.loads((folder / "definition.pbism").read_text(encoding="utf-8"))["version"] == "4.0"
    for document in ("database.tmdl", "model.tmdl", "relationships.tmdl", "tables/Sales.tmdl"):
        assert (definition / document).is_file(), document
    assert any((definition / "roles").glob("*.tmdl"))


def test_pbip_export_matches_the_single_document_tmdl_export(tmp_path, monkeypatch):
    monkeypatch.setenv("OSSIE_MICROSOFT_TOM_ASSEMBLIES", os.fspath(ASSEMBLIES))
    original = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    ossie = convert_semantic_model_to_ossie(original)

    folder = export_powerbi_project(ossie, tmp_path)
    from_folder = _read_definition_folder(folder / "definition")
    from_document = convert_semantic_model_to_ossie(
        convert_ossie_to_semantic_model(ossie, output_format="TMDL")
    )

    assert validate_tmsl(from_folder, assembly_dir=ASSEMBLIES).is_valid
    assert yaml.safe_load(convert_semantic_model_to_ossie(from_folder)) == yaml.safe_load(
        from_document
    )


def test_pbip_export_with_force_drops_documents_of_removed_tables(tmp_path):
    original = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    ossie = yaml.safe_load(convert_semantic_model_to_ossie(original))
    folder = export_powerbi_project(ossie, tmp_path, assembly_dir=ASSEMBLIES)
    (folder / "diagramLayout.json").write_text("{}", encoding="utf-8")
    assert (folder / "definition" / "tables" / "Customer.tmdl").is_file()

    # Customer is the "one" side of a relationship, so drop that relationship with it.
    ossie["datasets"] = [d for d in ossie["datasets"] if d["name"] != "Customer"]
    ossie["relationships"] = [
        r for r in ossie["relationships"] if "Customer" not in (r["from"], r["to"])
    ]
    export_powerbi_project(ossie, tmp_path, force=True, assembly_dir=ASSEMBLIES)

    assert not (folder / "definition" / "tables" / "Customer.tmdl").exists()
    assert (folder / "diagramLayout.json").is_file()
    tables = {t["name"] for t in _read_definition_folder(folder / "definition")["model"]["tables"]}
    assert "Customer" not in tables
    assert "Sales" in tables
