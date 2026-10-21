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

"""Shared fixtures: where the spec and the examples live, and a schema check."""

import json
import shutil
from pathlib import Path

import pytest

from ossie import OssieDocument

# The repository root holds `ossie`, the apache/ossie checkout the converters
# build and test against (a git submodule; see the root README).
REPO_ROOT = Path(__file__).resolve().parents[2]
OSSIE_ROOT = REPO_ROOT / "ossie"
TPCDS_PATH = OSSIE_ROOT / "examples" / "tpcds_semantic_model.yaml"
SCHEMA_PATH = OSSIE_ROOT / "core-spec" / "ossie-schema.json"

LIGHTDASH_CLI = shutil.which("lightdash")

needs_lightdash_cli = pytest.mark.skipif(
    LIGHTDASH_CLI is None,
    reason="the Lightdash CLI is not installed (npm install -g @lightdash/cli)",
)


@pytest.fixture(scope="session")
def ossie_schema():
    pytest.importorskip("jsonschema")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def assert_valid_ossie(ossie_schema):
    """Check a document against the spec's JSON Schema, the way
    `ossie validate` would, so a round trip cannot produce a document the
    Python models accept but the spec does not."""
    jsonschema = pytest.importorskip("jsonschema")

    def check(document: OssieDocument) -> None:
        payload = document.model_dump(mode="json", by_alias=True, exclude_none=True)
        jsonschema.validate(payload, ossie_schema)

    return check
