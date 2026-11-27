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

"""`slayer ossie-roundtrip` returns the core examples with every named element intact."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ["ossie/examples/tpcds_semantic_model.yaml", "ossie/examples/flights.semantic_model.yaml"]


def _names(document: dict) -> dict[str, list]:
    datasets = document.get("datasets") or []
    return {
        "datasets": sorted(d["name"] for d in datasets),
        "fields": sorted((d["name"], f["name"]) for d in datasets for f in d.get("fields") or []),
        "metrics": sorted(m["name"] for m in document.get("metrics") or []),
        "relationships": sorted(r["name"] for r in document.get("relationships") or []),
    }


@pytest.mark.parametrize("example", EXAMPLES)
def test_roundtrip_keeps_every_named_element(tmp_path: Path, example: str) -> None:
    source = ROOT / example
    output = tmp_path / "out.yaml"
    slayer = shutil.which("slayer")
    assert slayer is not None, "run inside the converter's uv environment"
    subprocess.run([slayer, "ossie-roundtrip", str(source), str(output)], check=True)
    assert _names(yaml.safe_load(output.read_text())) == _names(yaml.safe_load(source.read_text()))
