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

"""Every converter output validates against the Ossie JSON schema; TPC-DS reaches a fixpoint."""

import json
import re
from pathlib import Path

import pytest
import yaml

from ossie_tableau import convert as C
from ossie_tableau.cli import main

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2] / "ossie"
SCHEMA = json.loads((REPO / "core-spec" / "ossie-schema.json").read_text())
FEATURES = Path(__file__).parent / "fixtures" / "features"
TPCDS = REPO / "examples" / "tpcds_semantic_model.yaml"
OK_CASES = [
    p for p in sorted(FEATURES.iterdir())
    if p.is_dir() and not json.loads((p / "expected.json").read_text())["fail"]
]


def _validate(text: str) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(SCHEMA).validate(yaml.safe_load(text))


_NAMES = re.compile(r'"(source_file|datasource)": "[^"]*"')


def _blank(node):
    if isinstance(node, str):
        return _NAMES.sub(r'"\1": "-"', node)
    if isinstance(node, list):
        return [_blank(x) for x in node]
    if isinstance(node, dict):
        return {k: _blank(v) for k, v in node.items()}
    return node


def _stable(path: Path) -> dict:
    """Parsed document with the file-derived names (which grow each pass) blanked out."""
    doc = _blank(yaml.safe_load(path.read_text()))
    doc["name"] = "-"
    return doc


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_feature_fixture_outputs_validate(case):
    src = next(p for p in case.iterdir() if p.name.startswith("input."))
    for r in C.convert_file(src):
        if r.document is not None:
            _validate(r.document.to_ossie_yaml())


def test_tpcds_roundtrip_reaches_a_fixpoint_and_validates(tmp_path):
    def to_tds(model: Path, out: Path) -> Path:
        assert main(["to-tds", str(model), "-o", str(out), "--server", "example.snowflakecomputing.com",
                     "--database", "TPCDS"]) == 0
        return next(out.glob("*.tds"))

    def to_yaml(tds: Path, out: Path) -> Path:
        assert main(["convert", str(tds), "-o", str(out), "-q"]) == 0
        return next(out.glob("*.ossie.yaml"))

    t1 = to_tds(TPCDS, tmp_path / "t1")
    y1 = to_yaml(t1, tmp_path / "y1")
    t2 = to_tds(y1, tmp_path / "t2")
    y2 = to_yaml(t2, tmp_path / "y2")
    t3 = to_tds(y2, tmp_path / "t3")
    y3 = to_yaml(t3, tmp_path / "y3")
    _validate(y1.read_text())
    assert _stable(y3) == _stable(y2)
