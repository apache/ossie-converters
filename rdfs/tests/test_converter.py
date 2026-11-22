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


"""Tests for OssieToRdfsConverter and RdfsToOssieConverter.

The RDFS and the Ossie documents are made by ohi LinkML, so these tests do not
pin their exact text. They check the parts this package is responsible for
(options, warnings, the entry points, the spec -> model step) and the shape
of the mapping in both directions, reading RDF back with rdflib."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_ontology.model import OssieOntology
from ossie_ontology.spec import OssieSpec

from ossie_rdfs import OssieToRdfsConverter, RdfsToOssieConverter

FLIGHTS = Namespace("https://example.org/flights/")
PEOPLE = Namespace("https://example.org/people/")
# Not rdflib's XSD namespace: pytest pokes at module attributes while
# collecting, and that namespace warns on every unknown one.
XSD_DECIMAL = URIRef("http://www.w3.org/2001/XMLSchema#decimal")
XSD_STRING = URIRef("http://www.w3.org/2001/XMLSchema#string")

# A small ontology with a subtype, which drops nothing on the way to RDFS.
PEOPLE_OSSIE = """\
version: 0.3.0.dev0
name: People
ontology:
- concept: Name
  type: ValueType
  extends: [String]
- concept: Person
  type: EntityType
  identify_by: [name]
  relationships:
  - name: name
    roles: [{concept: Name}]
    verbalizes: ['{Person} is called {Name}']
    multiplicity: OneToOne
  - name: knows
    roles: [{concept: Person}]
    verbalizes: ['{Person} knows {Person}']
- concept: Employee
  type: EntityType
  extends: [Person]
"""

# A small RDFS vocabulary: classes with a subclass, and properties with a
# datatype, a class, rdfs:Literal and no domain at all as their range.
LIBRARY_RDFS = """\
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX lib: <https://example.org/library/>

lib:Agent a rdfs:Class ;
  rdfs:comment "Someone who can write a book." .
lib:Person a rdfs:Class ;
  rdfs:subClassOf lib:Agent .
lib:Book a rdfs:Class .

lib:title a rdf:Property ;
  rdfs:label "has title" ;
  rdfs:comment "The title of the book." ;
  rdfs:domain lib:Book ;
  rdfs:range xsd:string .
lib:author a rdf:Property ;
  rdfs:label "is written by" ;
  rdfs:domain lib:Book ;
  rdfs:range lib:Agent .
lib:pages a rdf:Property ;
  rdfs:domain lib:Book ;
  rdfs:range xsd:integer .
lib:note a rdf:Property ;
  rdfs:domain lib:Book ;
  rdfs:range rdfs:Literal .
lib:related a rdf:Property .
"""


def _no_warnings(convert, *args, **kwargs):
    """Call *convert*, failing on any warning: only use it for input that
    crosses over in full, such as People and the library vocabulary."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return convert(*args, **kwargs)


def _people_rdfs(**options) -> Graph:
    text = _no_warnings(
        OssieToRdfsConverter.convert_spec,
        OssieSpec.load_yaml(PEOPLE_OSSIE),
        schema_id="https://example.org/people",
        **options,
    )
    return Graph().parse(data=text, format=options.get("output_format", "turtle"))


def _flights_rdfs(model: OssieOntology) -> str:
    # Flights has mappings and `derived_by` rules that RDFS cannot hold; the
    # warnings about them are checked in their own test.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return OssieToRdfsConverter.convert(model, schema_id="https://example.org/flights")


@pytest.fixture(scope="module")
def flights_rdfs(flights_model: OssieOntology) -> Graph:
    return Graph().parse(data=_flights_rdfs(flights_model), format="turtle")


@pytest.fixture(scope="module")
def library_spec() -> OssieSpec:
    return _no_warnings(RdfsToOssieConverter().convert_to_spec, LIBRARY_RDFS)


# Ossie -> RDFS


def test_every_entity_type_becomes_a_class(flights_model: OssieOntology, flights_rdfs: Graph):
    entity_types = {
        c.concept for c in OssieToSpecConverter.convert(flights_model).ontology
        if c.type == "EntityType"
    }
    classes = set(flights_rdfs.subjects(RDF.type, RDFS.Class))
    # CancelationCode is a value type that lists its values, so it is a LinkML
    # enum on the way: a class with the values as its instances.
    assert classes == {FLIGHTS[name] for name in entity_types} | {FLIGHTS.CancelationCode}
    assert len(set(flights_rdfs.subjects(RDF.type, FLIGHTS.CancelationCode))) == 4


def test_relationship_becomes_a_property(flights_rdfs: Graph):
    latitude = FLIGHTS["Airport.latitude"]
    assert (latitude, RDF.type, RDF.Property) in flights_rdfs
    assert flights_rdfs.value(latitude, RDFS.domain) == FLIGHTS.Airport
    # DegreesLatitude is a value type; RDFS gets the datatype it is built on.
    assert flights_rdfs.value(latitude, RDFS.range) == XSD_DECIMAL
    # The text of the verbalization between the concepts becomes the label.
    assert str(flights_rdfs.value(latitude, RDFS.label)) == "centers at"

    # A relationship to an entity type has that entity's class as its range.
    assert flights_rdfs.value(FLIGHTS["Airport.city"], RDFS.range) == FLIGHTS.City


def test_subtype_becomes_a_subclass():
    rdfs = _people_rdfs()
    assert (PEOPLE.Employee, RDFS.subClassOf, PEOPLE.Person) in rdfs
    assert rdfs.value(PEOPLE["Person.name"], RDFS.range) == XSD_STRING
    assert rdfs.value(PEOPLE["Person.knows"], RDFS.range) == PEOPLE.Person
    # RDFS has no counts: nothing of OWL's is added to say "one value".
    assert not set(rdfs.subjects(RDF.type, OWL.FunctionalProperty))


def test_what_rdfs_cannot_hold_is_warned_about(flights_model: OssieOntology):
    with pytest.warns(UserWarning) as record:
        OssieToRdfsConverter.convert(flights_model)
    # Only keywords are matched, as ohi may reword its messages in any release.
    messages = [str(w.message).lower() for w in record]
    assert any("mapping" in m for m in messages)
    assert any("derived_by" in m for m in messages)
    # The warnings point at the caller, not at this package.
    assert all(w.filename == __file__ for w in record)


def test_n_triples():
    text = _no_warnings(
        OssieToRdfsConverter.convert_spec,
        OssieSpec.load_yaml(PEOPLE_OSSIE),
        schema_id="https://example.org/people",
        output_format="nt",
    )
    assert "PREFIX" not in text
    assert Graph().parse(data=text, format="nt").isomorphic(_people_rdfs())


def test_convert_and_convert_spec_agree(flights_model: OssieOntology):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from_spec = OssieToRdfsConverter.convert_spec(
            OssieToSpecConverter.convert(flights_model),
            schema_id="https://example.org/flights",
        )
    # Compared as graphs: the order of the triples in the text is not fixed.
    assert Graph().parse(data=from_spec, format="turtle").isomorphic(
        Graph().parse(data=_flights_rdfs(flights_model), format="turtle")
    )


# RDFS -> Ossie


def _concept(spec: OssieSpec, name: str):
    return next(c for c in spec.ontology if c.concept == name)


def _relationships(spec: OssieSpec, concept: str) -> dict:
    return {r.name: r for r in _concept(spec, concept).relationships}


def test_classes_become_entity_types(library_spec: OssieSpec):
    assert {c.concept for c in library_spec.ontology} == {"Agent", "Person", "Book"}
    assert all(c.type == "EntityType" for c in library_spec.ontology)
    assert _concept(library_spec, "Person").extends == ["Agent"]
    assert _concept(library_spec, "Agent").description == "Someone who can write a book."
    assert _concept(library_spec, "Book").iri == "lib:Book"
    assert library_spec.prefixes == {"lib": "https://example.org/library/"}


def test_properties_become_relationships_of_their_domain(library_spec: OssieSpec):
    book = _relationships(library_spec, "Book")
    assert set(book) == {"title", "author", "pages", "note"}
    # The range picks the role's concept: a built-in value type for a
    # datatype, the entity type for a class, and Any for rdfs:Literal.
    assert {name: [r.concept for r in rel.roles] for name, rel in book.items()} == {
        "title": ["String"],
        "author": ["Agent"],
        "pages": ["Integer"],
        "note": ["Any"],
    }
    # The label fills the verbalization, and the comment the description.
    assert book["author"].verbalizes == ["{Book} is written by {Agent}"]
    assert book["title"].description == "The title of the book."
    # RDFS cannot say that a property has one value.
    assert all(rel.multiplicity is None for rel in book.values())


def test_property_without_a_domain_is_dropped(library_spec: OssieSpec):
    # An Ossie relationship belongs to a concept, so `lib:related` has nowhere
    # to go.
    names = {r.name for c in library_spec.ontology for r in c.relationships}
    assert "related" not in names


def test_what_ossie_cannot_hold_is_warned_about():
    document = LIBRARY_RDFS + "lib:mobyDick a lib:Book .\n"
    with pytest.warns(UserWarning) as record:
        RdfsToOssieConverter().convert_text(document)
    assert any("individual" in str(w.message).lower() for w in record)
    assert all(w.filename == __file__ for w in record)


def test_runtime_model_is_built(tmp_path: Path):
    path = tmp_path / "library.ttl"
    path.write_text(LIBRARY_RDFS, encoding="utf-8")
    model = _no_warnings(RdfsToOssieConverter().convert_file, path)
    assert isinstance(model, OssieOntology)
    assert [c.name for c in model.ontology.lookup_concept("Person").extends] == ["Agent"]
    assert model.ontology.lookup_concept("Book").is_entity_type


def test_n_triples_input(library_spec: OssieSpec):
    n_triples = Graph().parse(data=LIBRARY_RDFS, format="turtle").serialize(format="nt")
    from_nt = _no_warnings(RdfsToOssieConverter().convert_to_spec, n_triples)
    assert {c.concept for c in from_nt.ontology} == {c.concept for c in library_spec.ontology}


def test_ontology_name():
    convert = RdfsToOssieConverter().convert_to_spec
    # Without an rdfs:label for the vocabulary, the prefix of its terms.
    assert _no_warnings(convert, LIBRARY_RDFS).name == "lib"
    assert _no_warnings(convert, LIBRARY_RDFS, name="Books").name == "Books"


# Both ways


def test_round_trip_keeps_classes_subtypes_and_properties():
    rdfs = _no_warnings(
        OssieToRdfsConverter.convert_spec,
        OssieSpec.load_yaml(PEOPLE_OSSIE),
        schema_id="https://example.org/people",
    )
    back = _no_warnings(RdfsToOssieConverter().convert_to_spec, rdfs)
    assert {c.concept for c in back.ontology} == {"Person", "Employee"}
    assert _concept(back, "Employee").extends == ["Person"]
    person = _relationships(back, "Person")
    # Value types come back as the built-in they were built on, and the
    # names are made from the property IRIs, which carry the concept.
    assert {name: [r.concept for r in rel.roles] for name, rel in person.items()} == {
        "person_name": ["String"],
        "person_knows": ["Person"],
    }
