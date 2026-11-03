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

"""The compliance fixture pair: sap-bp-slice.csn.json -> sap-bp-slice.ossie.yaml.

The pair is runnable independently of this implementation — the CSN document,
the curation overlay and the expected ontology are all plain data. These tests
assert that converting the left file produces the right one.
"""

from __future__ import annotations

import pytest
import yaml
from _util import canonical, describe_difference
from conftest import FIXTURES

from ossie_sap_csn_ontology import convert, dump_yaml, load_csn, load_curation, load_yaml, render


@pytest.fixture(scope="session")
def expected():
    with open(FIXTURES / "sap-bp-slice.ossie.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_generated_matches_the_expected_ontology(generated, expected):
    differences = describe_difference(canonical(generated), canonical(expected))
    assert differences == [], "generated ontology differs from the fixture:\n" + "\n".join(differences)


def test_every_expected_concept_is_generated(generated, expected):
    assert {c["concept"] for c in generated["ontology"]} == {
        c["concept"] for c in expected["ontology"]
    }


def test_relationships_match_concept_by_concept(generated, expected):
    def relationships(document):
        return {
            component["concept"]: {r["name"] for r in component.get("relationships", [])}
            for component in document["ontology"]
        }

    assert relationships(generated) == relationships(expected)


def test_conversion_is_deterministic():
    """Same input, same bytes: nothing time- or environment-dependent is emitted."""
    outputs = {
        dump_yaml(
            render(
                convert(
                    load_csn(FIXTURES / "sap-bp-slice.csn.json"),
                    load_curation(FIXTURES / "sap-bp-slice.curation.yaml"),
                )
            )
        )
        for _ in range(3)
    }
    assert len(outputs) == 1


def test_emitted_yaml_reloads_unchanged(generated):
    assert canonical(load_yaml(dump_yaml(generated))) == canonical(generated)


def test_emitted_yaml_uses_no_anchors(generated):
    """Aliases are valid YAML but make a fixture hard to read and diff."""
    text = dump_yaml(generated)
    assert " &" not in text and " *" not in text


def test_every_warning_is_a_declared_known_loss(slice_conversion):
    """Lengths and non-label annotations are reported, and nothing else is."""
    lengths = [w for w in slice_conversion.warnings if "cannot express a length constraint" in w]
    annotations = [w for w in slice_conversion.warnings if "annotation(s) have no home" in w]
    assert len(lengths) + len(annotations) == len(slice_conversion.warnings)
    assert slice_conversion.dropped_annotations == [
        "sap.slice.BusinessPartner: @ODM.entityName",
        "sap.slice.BusinessPartner.BusinessPartner: @Semantics.businessPartner.id",
        "sap.slice.Customer: @ODM.entityName",
    ]
    reported = {w.split(":")[0].removeprefix("value type ") for w in lengths}
    assert reported == {
        "BusinessPartnerNumber", "PartnerName", "CountryCode", "CustomerNumber",
        "SalesOrganization", "DistributionChannel", "Division",
    }


def test_curation_overlay_has_no_stale_entries():
    """Every overlay entry must still match something in the CSN."""
    curation = load_curation(FIXTURES / "sap-bp-slice.curation.yaml")
    convert(load_csn(FIXTURES / "sap-bp-slice.csn.json"), curation)
    assert curation.unused_keys() == []
