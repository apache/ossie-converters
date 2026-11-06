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
from pathlib import Path

import pytest

from ossie_tableau import convert as C
from ossie_tableau.losses import KNOWN_KINDS

from tests.snapshots import loss_counts, snapshot

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parent / "fixtures" / "features"
CASES = sorted(p for p in ROOT.iterdir() if p.is_dir())
OK_CASES = [p for p in CASES if not json.loads((p / "expected.json").read_text())["fail"]]


def _src(case):
    return next(p for p in case.iterdir() if p.name.startswith("input."))


@pytest.mark.parametrize("case", CASES, ids=lambda p: p.name)
def test_feature_fixture(case):
    exp = json.loads((case / "expected.json").read_text())
    src = _src(case)
    if exp["fail"]:
        with pytest.raises(Exception) as ei:
            C.convert_file(src)
        assert exp["fail"] in f"{type(ei.value).__name__}: {ei.value}"
        return
    res = C.convert_file(src)
    assert len(res) == exp["outputs"]
    kinds = {x.kind for r in res for x in r.losses}
    assert kinds == set(exp["loss_kinds"])
    for r in res:
        if r.document is not None:
            assert r.document.to_ossie_yaml()


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_feature_fixture_per_datasource_content(case):
    exp = json.loads((case / "expected.json").read_text())
    res = C.convert_file(_src(case))
    got = {
        r.datasource: {
            **{k: (r.stats or {}).get(k, 0) for k in ("datasets", "fields", "relationships", "metrics")},
            "converted": r.document is not None,
            "losses": sorted({x.kind for x in r.losses}),
        }
        for r in res
    }
    assert got == exp["datasources"]


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_feature_fixture_losses_are_catalogued_and_classified(case):
    for r in C.convert_file(_src(case)):
        for x in r.losses:
            assert x.kind in KNOWN_KINDS, x.kind
            assert x.category and x.severity and x.remedy, x.kind


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_feature_fixture_output_is_deterministic(case):
    src = _src(case)
    a = [r.document.to_ossie_yaml() for r in C.convert_file(src) if r.document is not None]
    b = [r.document.to_ossie_yaml() for r in C.convert_file(src) if r.document is not None]
    assert a == b


def test_twbx_matches_twb():
    twb = C.convert_file(_src(ROOT / "tpch_multi"))
    twbx = C.convert_file(_src(ROOT / "tpch_multi_twbx"))
    assert [r.datasource for r in twb] == [r.datasource for r in twbx]
    assert [r.document.to_ossie_yaml() for r in twb if r.document] == [
        r.document.to_ossie_yaml().replace("input.twbx", "input.twb") for r in twbx if r.document
    ]
    assert [sorted((x.kind, x.name) for x in r.losses) for r in twb] == [
        sorted((x.kind, x.name) for x in r.losses) for r in twbx
    ]


def test_bigquery_wildcard_source_is_kept_raw_and_columns_come_from_map():
    r = C.convert_file(_src(ROOT / "bigquery_constructs"))[0]
    yaml = r.document.to_ossie_yaml()
    assert "source: proj.ds.events_*" in yaml
    assert (r.stats or {})["fields"] == 2


def test_tpch_multi_cross_table_metric_is_fully_qualified():
    docs = [r.document.to_ossie_yaml() for r in C.convert_file(_src(ROOT / "tpch_multi"))]
    assert docs
    for yaml in docs:
        assert "SUM((LINEITEM.L_EXTENDEDPRICE) * (1 - (LINEITEM.L_DISCOUNT)))" in yaml
        assert "COUNT(DISTINCT (CUSTOMER.C_CUSTKEY))" in yaml


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_feature_fixture_matches_golden(case):
    got = snapshot(case)
    gold = case / "expected"
    assert gold.is_dir(), "run tools/update_snapshots.py"
    assert sorted(p.name for p in gold.iterdir()) == sorted(got)
    for name, text in got.items():
        assert (gold / name).read_text() == text, f"{case.name}/{name} differs; run tools/update_snapshots.py and read the diff"


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_feature_fixture_loss_counts(case):
    exp = json.loads((case / "expected.json").read_text())
    assert loss_counts(case) == exp["loss_counts"]
