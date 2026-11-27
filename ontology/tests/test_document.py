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
"""Tests for what the converters carry between the shared `ossie.models`
documents, read and written with their own `from_ossie_*` / `to_ossie_*`, and
the runtime model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ossie.models import OssieConceptType, OssieOntologyModel

from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_ontology.converter.spec_to_ossie.converter import SpecToOssieConverter
from ossie_ontology.model import Concept, ConceptType, OssieOntology, Relationship

# The embedded mapping is unnamed and its field carries only the portable
# `sql` form, both of which the schema allows.
_DOCUMENT = """
version: 0.3.0.dev0
name: Sales
ontology:
- concept: Order
  type: EntityType
  identify_by: [id]
  relationships:
  - name: id
    roles:
    - concept: Integer
    verbalizes: ['{Order} has {Integer}']
    multiplicity: OneToOne
ontology_mappings:
- semantic_model:
    version: 0.3.0.dev0
    name: sales
    datasets:
    - name: ORDERS
      source: sales.public.orders
      fields:
      - name: id
        expression:
          sql: order_id
        datatype: Integer
  concept_mappings:
  - concept: Order
    object_mappings:
    - referent_mappings:
      - relationship: id
        expression: ORDERS.id
"""


def _load() -> OssieOntologyModel:
    return OssieOntologyModel.from_ossie_yaml(_DOCUMENT)


def _export(model: OssieOntology) -> dict:
    return json.loads(OssieToSpecConverter.convert(model).to_ossie_json())


def test_converters_round_trip_the_document():
    document = _load()

    exported = OssieToSpecConverter.convert(SpecToOssieConverter().convert(document))

    # The unnamed mapping comes back named; see the last test.
    named = [mapping.model_copy(update={"name": "sales"}) for mapping in document.ontology_mappings]
    assert OssieOntologyModel.from_ossie_yaml(exported.to_ossie_yaml()) == document.model_copy(
        update={"ontology_mappings": named}
    )
    assert exported.ontology[0].type is OssieConceptType.ENTITY_TYPE


def test_export_omits_empty_optional_lists_but_keeps_required_ones():
    document = OssieOntologyModel.from_ossie_yaml("""
version: 0.3.0.dev0
name: Empty
ontology:
- concept: Thing
  type: EntityType
  relationships:
  - name: flag
    verbalizes: []
""")

    exported = _export(SpecToOssieConverter().convert(document))

    assert exported == {
        "version": "0.3.0.dev0",
        "name": "Empty",
        "ontology": [{
            "concept": "Thing",
            "type": "EntityType",
            # Required by the schema, so an empty one is still written.
            "relationships": [{"name": "flag", "verbalizes": []}],
        }],
    }


def test_portable_sql_expression_survives_the_round_trip():
    model = SpecToOssieConverter().convert(_load())

    field = model.ontology_mappings[0].semantic_model.lookup_dataset("ORDERS").field("id")
    assert field.expression.sql == "order_id"
    assert field.expression.primary.dialect == "OSSIE_SQL_2026"
    assert field.expression.primary.expression == "order_id"

    exported = _export(model)
    dataset = exported["ontology_mappings"][0]["semantic_model"]["datasets"][0]
    assert dataset["fields"][0]["expression"] == {"sql": "order_id"}


def test_unnamed_ontology_mapping_takes_its_semantic_model_name():
    model = SpecToOssieConverter().convert(_load())

    assert [mapping.name for mapping in model.ontology_mappings] == ["sales"]


def test_annotations_survive_round_trip_on_every_ontology_node():
    document = OssieOntologyModel.from_ossie_yaml("""
version: 0.3.0.dev0
name: Annotated
annotations:
  dc:title: Annotated ontology
  meta:
    nested: true
    empty: null
ontology:
- concept: Person
  type: EntityType
  annotations:
    rdfs:label: Person
  relationships:
  - name: knows
    roles:
    - concept: Person
      name: acquaintance
      annotations:
        custom:role_tag: friend
    verbalizes: ['{Person} knows {Person:acquaintance}']
    annotations:
      vs:term_status: stable
  - name: nickname
    roles:
    - concept: String
      annotations: {}
    verbalizes: ['{Person} has nickname {String}']
    annotations: {}
""")

    model = SpecToOssieConverter().convert(document)
    assert model.annotations == {"dc:title": "Annotated ontology", "meta": {"nested": True, "empty": None}}
    person = model.ontology.lookup_concept("Person")
    assert person is not None and person.annotations == {"rdfs:label": "Person"}
    knows = model.ontology.lookup_concept_relationship(person, "knows")
    assert knows is not None and knows.annotations == {"vs:term_status": "stable"}
    assert knows.roles[0].annotations is None
    assert knows.roles[1].annotations == {"custom:role_tag": "friend"}
    nickname = model.ontology.lookup_concept_relationship(person, "nickname")
    assert nickname is not None and nickname.annotations == {}
    assert nickname.roles[1].annotations == {}

    exported = OssieToSpecConverter.convert(model)
    assert OssieOntologyModel.from_ossie_yaml(exported.to_ossie_yaml()) == document


def test_foaf_example_with_annotations_round_trips():
    example = Path(__file__).resolve().parents[2] / "ossie" / "examples" / "foaf_owl_import.yaml"
    if not example.is_file():
        pytest.skip(f"example not present at {example}")

    document = OssieOntologyModel.from_ossie_yaml(example.read_text(encoding="utf-8"))
    exported = OssieToSpecConverter.convert(SpecToOssieConverter().convert(document))

    assert OssieOntologyModel.from_ossie_yaml(exported.to_ossie_yaml()) == document


def test_relationship_rejects_mismatched_role_annotations():
    person = Concept(name="Person", type=ConceptType.ENTITY_TYPE)
    with pytest.raises(ValueError, match="role_annotations"):
        Relationship(
            name="knows",
            container=person,
            relates=[(person, None)],
            role_annotations=[{"a": 1}, {"b": 2}],
        )
