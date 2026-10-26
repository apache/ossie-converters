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
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ossie_tableau import convert as C
from tests._tds_norm import normalize, yaml_to_tds

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parent / "fixtures" / "features"
BQ = ["--warehouse-type", "bigquery", "--project", "proj", "--server", "bigquery.googleapis.com",
      "--database", "proj", "--schema", "ds"]
SF = ["--server", "account.snowflakecomputing.com", "--warehouse", "WH", "--database", "DB", "--schema", "SCH"]
# Fixtures whose connection class has no writer: to-tds needs an explicit Snowflake target.
EXTRA = {"bigquery_constructs": BQ, "blend": SF, "unknown_warehouse": SF, "multi_inner": SF}


def _src(name):
    return next(p for p in (ROOT / name).iterdir() if p.name.startswith("input."))


def _docs(name):
    return [r for r in C.convert_file(_src(name)) if r.document is not None]


def _cases():
    out = []
    for p in sorted(ROOT.iterdir()):
        if json.loads((p / "expected.json").read_text())["fail"]:
            continue
        for r in _docs(p.name):
            out.append(pytest.param(p.name, r.datasource, id=f"{p.name}-{r.datasource}"))
    return out


CASES = _cases()


def _doc(name, ds):
    return next(r for r in _docs(name) if r.datasource == ds)


@pytest.mark.parametrize("name,ds", CASES)
def test_to_tds_succeeds_and_is_well_formed(name, ds, tmp_path):
    rc, tds = yaml_to_tds(_doc(name, ds).document.to_ossie_yaml(), tmp_path, EXTRA.get(name))
    assert rc == 0
    root = ET.parse(tds).getroot()
    assert root.tag == "datasource"
    assert list(root.iter("relation"))


@pytest.mark.parametrize("name,ds", CASES)
def test_round_trip_never_writes_a_connection_customization(name, ds, tmp_path):
    _, tds = yaml_to_tds(_doc(name, ds).document.to_ossie_yaml(), tmp_path, EXTRA.get(name))
    assert "connection-customization" not in tds.read_text()


@pytest.mark.parametrize("name,ds", CASES)
def test_round_trip_reaches_a_fixpoint(name, ds, tmp_path):
    extra = EXTRA.get(name)
    _, t1 = yaml_to_tds(_doc(name, ds).document.to_ossie_yaml(), tmp_path / "p1", extra)
    y2 = C.convert_file(t1)[0].document.to_ossie_yaml()
    _, t2 = yaml_to_tds(y2, tmp_path / "p2", extra)
    y3 = C.convert_file(t2)[0].document.to_ossie_yaml()
    assert y3 == y2


@pytest.mark.parametrize("name,ds", CASES)
def test_round_trip_keeps_dataset_and_field_counts(name, ds, tmp_path):
    first = _doc(name, ds)
    _, tds = yaml_to_tds(first.document.to_ossie_yaml(), tmp_path, EXTRA.get(name))
    back = C.convert_file(tds)[0]
    assert back.document is not None
    for k in ("datasets", "relationships"):
        assert back.stats[k] == first.stats[k], k
    assert back.stats["fields"] >= first.stats["fields"]


@pytest.mark.parametrize("name", ["tpch_multi", "tpch_multi_twbx"])
def test_tpch_multi_round_trip_preserves_connection_and_tables(name, tmp_path):
    for r in _docs(name):
        _, tds = yaml_to_tds(r.document.to_ossie_yaml(), tmp_path / r.datasource, EXTRA.get(name))
        n = normalize(tds.read_text())
        assert n["connection"]["class"] == "snowflake"
        assert n["connection"]["server"] == "account.snowflakecomputing.com"
        assert {t for _, t in n["relations"]}
        for relation in ET.parse(tds).getroot().iter("relation"):
            if relation.get("type") == "table":
                assert relation.get("table").count("].[") == 2


def test_bigquery_constructs_round_trip_keeps_wildcard_and_query_parameter(tmp_path):
    _, tds = yaml_to_tds(_doc("bigquery_constructs", "bq").document.to_ossie_yaml(), tmp_path, BQ)
    text = tds.read_text()
    assert "class='bigquery'" in text or 'class="bigquery"' in text
    assert "events_*" in text
    assert "@start_date" in text


@pytest.mark.parametrize("name", ["blend", "unknown_warehouse", "multi_inner"])
def test_postgres_fixture_needs_an_explicit_target_for_to_tds(name, tmp_path):
    r = _docs(name)[0]
    with pytest.raises(SystemExit) as ei:
        yaml_to_tds(r.document.to_ossie_yaml(), tmp_path)
    assert ei.value.code == 2
