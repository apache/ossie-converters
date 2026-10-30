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

FIX = Path(__file__).parent / "fixtures"
BQ = FIX / "tpch_bigquery" / "tpch_semantic_model.tds"
BQ_YAML = FIX / "tpch_bigquery" / "tpch_semantic_model.yaml"
SF_YAML = FIX / "tpch_snowflake" / "tpch_semantic_model.yaml"
TABLES = {"LINEITEM", "ORDERS", "CUSTOMER", "NATION", "PART", "SUPPLIER"}
BQ_ARGS = ["--warehouse-type", "bigquery", "--server", "bigquery.googleapis.com",
           "--database", "my-gcp-project", "--schema", "my_dataset", "--project", "my-gcp-project"]


def _roundtrip(tds: Path, work: Path):
    (work / "in").mkdir(parents=True)
    src = work / "in" / "model.tds"
    src.write_text(tds.read_text())
    res = C.convert_file(src)[0]
    yml = work / "model.ossie.yaml"
    yml.write_text(res.document.to_ossie_yaml())
    assert main(["to-tds", str(yml), "-o", str(work), *BQ_ARGS]) == 0
    return res, yml, work / "model.tds"


def test_fixture_is_a_bigquery_tpch_datasource():
    root = ET.parse(BQ).getroot()
    assert {r.get("name") for r in root.find("connection/relation")} == TABLES
    assert len(list(root.iter("relationship"))) == 5
    classes = {c.get("class") for c in root.iter("connection") if c.get("class") != "federated"}
    assert classes == {"bigquery"}
    text = BQ.read_text()
    assert "snowflake" not in text.lower()


def test_fixture_converts_without_losses():
    res = C.convert_file(BQ)[0]
    assert {d.name for d in res.document.datasets} == TABLES
    assert not [x for x in res.losses if x.severity == "error"]
    assert not any(x.kind == "unknown_warehouse" for x in res.losses)


def test_bigquery_model_mirrors_the_snowflake_model():
    import yaml

    def shape(p):
        d = yaml.safe_load(p.read_text())
        return {ds["name"]: [f["name"] for f in ds.get("fields", [])] for ds in d["datasets"]}, \
            sorted(map(str, d.get("relationships", [])))

    assert shape(BQ_YAML)[0] == shape(SF_YAML)[0]
    assert len(shape(BQ_YAML)[1]) == len(shape(SF_YAML)[1]) == 5


def test_bigquery_tds_yaml_tds_reaches_a_fixpoint(tmp_path):
    _, _, t1 = _roundtrip(BQ, tmp_path / "p1")
    _, y2, t2 = _roundtrip(t1, tmp_path / "p2")
    _, y3, _ = _roundtrip(t2, tmp_path / "p3")
    assert y3.read_text() == y2.read_text()


def test_bigquery_roundtrip_preserves_meaning(tmp_path):
    from tests._tds_norm import normalize

    _, _, out = _roundtrip(BQ, tmp_path)
    a, b = normalize(BQ.read_text()), normalize(out.read_text())
    assert b["connection"] == a["connection"]
    assert b["relations"] == a["relations"]
    assert b["relationships"] == a["relationships"]
    for name, col in a["columns"].items():
        assert b["columns"].get(name) == col, name


def test_bigquery_tables_keep_three_part_paths_and_dfs_order(tmp_path):
    _, _, out = _roundtrip(BQ, tmp_path)
    root = ET.parse(out).getroot()
    paths = {r.get("table") for r in root.iter("relation") if r.get("type") == "table"}
    assert paths and all(re.fullmatch(r"\[[^\]]+\]\.\[[^\]]+\]\.\[ossie_it_tpch_\w+\]", p) for p in paths), paths
    assert [r.get("name") for r in root.find("connection/relation")] == \
        ["LINEITEM", "ORDERS", "CUSTOMER", "NATION", "PART", "SUPPLIER"]


def test_bigquery_roundtrip_never_invents_a_connection_customization(tmp_path):
    _, _, out = _roundtrip(BQ, tmp_path)
    assert "connection-customization" not in out.read_text()
