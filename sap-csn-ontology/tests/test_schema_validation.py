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

"""The generated ontology validates against the spec schema as shipped."""

from __future__ import annotations

import pytest
import yaml
from _util import ontology_validator, schema_errors
from conftest import FIXTURES, OSSIE_SUBMODULE

from ossie_sap_csn_ontology import ONTOLOGY_VERSION, dump_yaml, load_yaml, render


@pytest.fixture(scope="session")
def validator():
    return ontology_validator(OSSIE_SUBMODULE)


def test_generated_ontology_validates(validator, generated):
    assert schema_errors(validator, generated) == []


def test_generated_ontology_validates_after_a_yaml_round_trip(validator, generated):
    """Validation must hold for the file on disk, not just the in-memory dict."""
    reloaded = load_yaml(dump_yaml(generated))
    assert schema_errors(validator, reloaded) == []


def test_expected_fixture_validates(validator):
    """Guards the hand-written half of the fixture pair against spec drift."""
    with open(FIXTURES / "sap-bp-slice.ossie.yaml", encoding="utf-8") as handle:
        expected = yaml.safe_load(handle)
    assert schema_errors(validator, expected) == []


def test_ontology_without_a_curation_overlay_validates(validator, bare_conversion):
    """The overlay adds language, not validity: bare output is a valid ontology."""
    document = render(bare_conversion, name="sap_business_partner_slice_bare")
    assert schema_errors(validator, document) == []


def test_emitted_version_matches_the_schema_constant(validator):
    assert validator.schema["properties"]["version"]["const"] == ONTOLOGY_VERSION
