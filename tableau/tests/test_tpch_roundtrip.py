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

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ossie_tableau import convert as C
from ossie_tableau.cli import main

pytestmark = pytest.mark.unit

MODEL = Path(__file__).parent / "fixtures" / "tpch_snowflake" / "tpch_semantic_model.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "tpch_snowflake" / "tpch_semantic_model.tds"
TABLES = {"LINEITEM", "ORDERS", "CUSTOMER", "NATION", "PART", "SUPPLIER"}


def _roundtrip(tds: Path, work: Path):
    (work / "in").mkdir(parents=True)
    src = work / "in" / "model.tds"
    src.write_text(tds.read_text())
    res = C.convert_file(src)[0]
    yml = work / "model.ossie.yaml"
    yml.write_text(res.document.to_ossie_yaml())
    assert main(["to-tds", str(yml), "-o", str(work)]) == 0
    return res, yml, work / "model.tds"


def test_fixture_matches_model():
    text = FIXTURE.read_text()
    root = ET.fromstring(text)
    assert {r.get("name").upper() for r in root.find("connection/relation")} == TABLES
    assert len(list(root.iter("relationship"))) == 5
    assert "TPCH_SF1" in text.upper()


def test_fixture_converts_without_losses():
    res = C.convert_file(FIXTURE)[0]
    assert res.document is not None
    assert {d.name for d in res.document.datasets} == TABLES
    assert not [x for x in res.losses if x.severity == "error"]


def test_tpch_tds_yaml_tds_reaches_a_fixpoint(tmp_path):
    _, _, t1 = _roundtrip(FIXTURE, tmp_path / "p1")
    _, y2, t2 = _roundtrip(t1, tmp_path / "p2")
    _, y3, _ = _roundtrip(t2, tmp_path / "p3")
    assert y3.read_text() == y2.read_text()


def test_tpch_calculation_refs_resolve_to_declared_columns(tmp_path):
    _, _, out = _roundtrip(FIXTURE, tmp_path)
    root = ET.fromstring(out.read_text())
    declared = {c.get("name", "").strip("[]").upper() for c in root.findall("column")}
    checked = 0
    for c in root.findall("column"):
        calc = c.find("calculation")
        if calc is None or not calc.get("formula"):
            continue
        for ref in re.findall(r"\[([^\]]+)\]", calc.get("formula")):
            checked += 1
            assert ref.upper() in declared, f"{c.get('caption')}: [{ref}] is not declared"
    assert checked >= 4


def test_tpch_bigquery_roundtrip(tmp_path):
    assert main(["to-tds", str(MODEL), "-o", str(tmp_path), "--warehouse-type", "bigquery",
                 "--server", "bigquery.googleapis.com", "--database", "proj", "--schema", "ds", "--project", "proj"]) == 0
    tds = next(tmp_path.glob("*.tds"))
    assert "class='bigquery'" in tds.read_text() or 'class="bigquery"' in tds.read_text()
    back = C.convert_file(tds)
    assert back[0].document is not None
    assert not any(x.kind == "unknown_warehouse" for x in back[0].losses)


def test_tds_yaml_tds_preserves_meaning(tmp_path):
    from tests._tds_norm import normalize

    _, _, out = _roundtrip(FIXTURE, tmp_path)
    a, b = normalize(FIXTURE.read_text()), normalize(out.read_text())
    assert b["connection"] == a["connection"]
    assert b["relations"] == a["relations"]
    assert b["relationships"] == a["relationships"]
    for name, col in a["columns"].items():
        assert name in b["columns"], name
        assert b["columns"][name] == col, name


def test_round_tripped_tds_children_follow_tableau_schema_order(tmp_path):
    from ossie_tableau.tds_writer import _SCHEMA_ORDER

    _, _, out = _roundtrip(FIXTURE, tmp_path)
    ranks = [_SCHEMA_ORDER[c.tag.rsplit("...", 1)[-1]] for c in ET.fromstring(out.read_text())]
    assert ranks == sorted(ranks)


def test_round_tripped_tds_relationship_fields_resolve_through_cols(tmp_path):
    _, _, out = _roundtrip(FIXTURE, tmp_path)
    root = ET.fromstring(out.read_text())
    conn = root.find("connection")
    assert [r.get("type") for r in conn.findall("relation")] == ["collection"]
    maps = {m.get("key"): m.get("value") for m in conn.find("cols")}
    names = {r.get("name") for r in conn.find("relation")}
    assert {v.split("].")[0].lstrip("[") for v in maps.values()} <= names
    for rel in root.iter("relationship"):
        for e in rel.find("expression").findall("expression"):
            assert e.get("op") in maps, e.get("op")


def test_round_tripped_tds_collection_is_root_first_and_objects_have_table_columns(tmp_path):
    _, _, out = _roundtrip(FIXTURE, tmp_path)
    root = ET.fromstring(out.read_text())
    assert [x.get("name") for x in root.find("connection/relation")][0] == "LINEITEM"
    ids = {o.get("id"): o.get("caption") for o in root.iter("object")}
    tables = {c.get("name"): c.get("caption") for c in root.findall("column") if c.get("datatype") == "table"}
    assert tables == {f"[__tableau_internal_object_id__].[{i}]": cap for i, cap in ids.items()}


def test_round_tripped_tables_keep_three_part_paths(tmp_path):
    _, _, out = _roundtrip(FIXTURE, tmp_path)
    paths = {r.get("table") for r in ET.parse(out).getroot().iter("relation") if r.get("type") == "table"}
    assert paths and all(p.count("].[") == 2 for p in paths), paths


def test_round_tripped_collection_follows_relationship_depth_first_order(tmp_path):
    _, _, out = _roundtrip(FIXTURE, tmp_path)
    coll = ET.parse(out).getroot().find("connection/relation")
    assert [r.get("name") for r in coll] == ["LINEITEM", "ORDERS", "CUSTOMER", "NATION", "PART", "SUPPLIER"]


def test_round_trip_never_invents_a_connection_customization(tmp_path):
    _, _, out = _roundtrip(FIXTURE, tmp_path)
    assert "connection-customization" not in out.read_text()
    assert list(ET.parse(out).getroot().iter("connection-customization")) == []


def test_source_connection_customization_is_restored_unchanged(tmp_path):
    block = ("<connection-customization class='snowflake' enabled='true' version='1'><vendor name='snowflake'/>"
             "<driver name='snowflake'/><customizations><customization name='CAP_X' value='yes'/></customizations>"
             "</connection-customization>")
    src = tmp_path / "src.tds"
    src.write_text(re.sub(r"(<connection\b[^>]*class='snowflake'[^>]*?)\s*/>", lambda m: f"{m.group(1)}>{block}</connection>",
                          FIXTURE.read_text(), count=1))
    assert block in src.read_text()
    _, _, out = _roundtrip(src, tmp_path / "rt")
    blocks = list(ET.parse(out).getroot().iter("connection-customization"))
    assert len(blocks) == 1
    assert [(c.get("name"), c.get("value")) for c in blocks[0].iter("customization")] == [("CAP_X", "yes")]


def test_round_trip_does_not_invent_a_federated_caption(tmp_path):
    _, _, out = _roundtrip(FIXTURE, tmp_path)
    root = ET.parse(out).getroot()
    assert root.get("caption") is None
    assert root.get("formatted-name") == "federated.1b4cgxc0es9g0e1da4yec04i4bu9"
    assert root.get("source-platform") == "mac"


def test_authored_model_gets_its_name_as_caption_but_never_a_federated_one(tmp_path):
    _, yml, _ = _roundtrip(FIXTURE, tmp_path)
    text = yml.read_text()
    for name, want in (("Sales Model", "Sales Model"), ("federated_abc", None)):
        y = tmp_path / f"{want}.yaml"
        y.write_text(re.sub(r"(?m)^name: .*$", f"name: {name}", text, count=1))
        out = tmp_path / f"o_{want}"
        out.mkdir()
        assert main(["to-tds", str(y), "-o", str(out)]) == 0
        assert ET.parse(next(out.glob("*.tds"))).getroot().get("caption") == want
