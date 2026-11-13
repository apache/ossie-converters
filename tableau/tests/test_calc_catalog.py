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

import subprocess
import sys
from pathlib import Path

import pytest

from ossie_tableau.calc_catalog import BY_NAME, CATALOG

pytestmark = pytest.mark.unit
CONV = Path(__file__).resolve().parents[1]


def test_names_unique_and_indexed():
    assert len(BY_NAME) == len(CATALOG)


def test_statuses_and_notes():
    for e in CATALOG:
        assert e.status in {"direct", "approximated", "unmappable"}, e.name
        assert all(a >= 0 for a in e.arity), e.name
        if e.status != "direct":
            assert e.note, f"{e.name} needs an explanatory note"


def _gen_docs() -> Path:
    return CONV / "tools" / "gen_docs.py"


def test_expression_mapping_doc_is_current():
    r = subprocess.run([sys.executable, str(_gen_docs()), "--check"])
    assert r.returncode == 0, "run `python3 tools/gen_docs.py` to regenerate docs/"


def test_every_catalogue_entry_is_documented():
    text = (CONV / "docs/expression-mapping.md").read_text()
    for e in CATALOG:
        assert f"| `{e.name}` |" in text
