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


"""Tests for OssieToShaclConverter.

The SHACL itself is made by ohi LinkML, so these tests do not pin its exact
text. They read it back with rdflib, check the parts this package is
responsible for (options, warnings, both entry points), and run a SHACL
validator over small data graphs to check the shapes accept and reject what
they should."""

from __future__ import annotations

import warnings

import pytest
from pyshacl import validate
from rdflib import RDF, SH, Graph, Literal, Namespace, URIRef

from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_ontology.model import OssieOntology
from ossie_ontology.spec import OssieSpec

from ossie_shacl import OssieToShaclConverter

FLIGHTS = Namespace("https://example.org/flights/")
PEOPLE = Namespace("https://example.org/people/")
# Not rdflib's XSD namespace: pytest pokes at module attributes while
# collecting, and that namespace warns on every unknown one.
XSD_DECIMAL = URIRef("http://www.w3.org/2001/XMLSchema#decimal")

# A small ontology with a subtype, a mandatory relationship and an optional one.
PEOPLE_OSSIE = """\
version: 0.3.0.dev0
name: People
ontology:
- concept: Name
  type: ValueType
  extends: [String]
- concept: Salary
  type: ValueType
  extends: [Decimal]
- concept: Person
  type: EntityType
  identify_by: [name]
  relationships:
  - name: name
    roles: [{concept: Name}]
    verbalizes: ['{Person} is called {Name}']
    multiplicity: OneToOne
- concept: Employee
  type: EntityType
  extends: [Person]
  relationships:
  - name: earns
    roles: [{concept: Salary}]
    verbalizes: ['{Employee} earns {Salary}']
    multiplicity: ManyToOne
"""


def _convert(spec: OssieSpec, **options) -> Graph:
    """Convert People, which drops nothing, so no warnings are expected."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        text = OssieToShaclConverter.convert_spec(
            spec, schema_id="https://example.org/people", **options
        )
    return Graph().parse(data=text, format="turtle")


def _flights(model: OssieOntology, **options) -> str:
    # Flights has mappings and `derived_by` rules that SHACL cannot hold; the
    # warnings about them are checked in their own test.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return OssieToShaclConverter.convert(
            model, schema_id="https://example.org/flights", **options
        )


def _property_shape(shapes: Graph, node_shape: URIRef, path: URIRef):
    matches = [
        p for p in shapes.objects(node_shape, SH.property)
        if shapes.value(p, SH.path) == path
    ]
    assert len(matches) == 1, f"expected one property shape for {path} on {node_shape}"
    return matches[0]


@pytest.fixture(scope="module")
def people_spec() -> OssieSpec:
    return OssieSpec.load_yaml(PEOPLE_OSSIE)


@pytest.fixture(scope="module")
def flights_shapes(flights_model: OssieOntology) -> Graph:
    return Graph().parse(data=_flights(flights_model), format="turtle")


def test_every_entity_type_gets_a_node_shape(flights_model: OssieOntology, flights_shapes: Graph):
    entity_types = {
        c.concept for c in OssieToSpecConverter.convert(flights_model).ontology
        if c.type == "EntityType"
    }
    shapes = set(flights_shapes.subjects(RDF.type, SH.NodeShape))
    assert shapes == {FLIGHTS[name] for name in entity_types}
    for shape in shapes:
        # Each shape targets the class of the same IRI.
        assert flights_shapes.value(shape, SH.targetClass) == shape


def test_relationship_becomes_a_property_shape(flights_shapes: Graph):
    # `requires: [Airport.latitude, ...]` makes it mandatory, ManyToOne caps it
    # at one, and DegreesLatitude's `requires` bounds become the value range.
    latitude = _property_shape(flights_shapes, FLIGHTS.Airport, FLIGHTS["Airport.latitude"])
    assert flights_shapes.value(latitude, SH.minCount) == Literal(1)
    assert flights_shapes.value(latitude, SH.maxCount) == Literal(1)
    assert flights_shapes.value(latitude, SH.datatype) == XSD_DECIMAL
    assert flights_shapes.value(latitude, SH.minInclusive).toPython() == -90
    assert flights_shapes.value(latitude, SH.maxInclusive).toPython() == 90

    # A relationship to an entity type points at that entity's class.
    city = _property_shape(flights_shapes, FLIGHTS.Airport, FLIGHTS["Airport.city"])
    assert flights_shapes.value(city, SH["class"]) == FLIGHTS.City


def test_string_enumeration_becomes_sh_in(flights_shapes: Graph):
    # `CancelationCode == 'A' OR ...` becomes a LinkML enum, and its values are
    # IRIs, as in ohi's RDFS and OWL output (individuals of the enum's class).
    code = _property_shape(
        flights_shapes, FLIGHTS.CancelationReason, FLIGHTS["CancelationReason.code"]
    )
    members = list(flights_shapes.items(flights_shapes.value(code, SH["in"])))
    assert members == [FLIGHTS[f"CancelationCode.{c}"] for c in "ABCD"]


def test_what_shacl_cannot_hold_is_warned_about(flights_model: OssieOntology):
    with pytest.warns(UserWarning) as record:
        OssieToShaclConverter.convert(flights_model)
    messages = [str(w.message) for w in record]
    assert any("Ontology mapping, which is not read" in m for m in messages)
    assert any("Derivation (`derived_by`)" in m for m in messages)
    # The warnings point at the caller, not at this package.
    assert all(w.filename == __file__ for w in record)


def test_shapes_are_open_by_default(people_spec: OssieSpec):
    shapes = _convert(people_spec)
    closed = {shapes.value(s, SH.closed) for s in shapes.subjects(RDF.type, SH.NodeShape)}
    assert closed == {Literal(False)}


def test_shapes_can_be_closed(people_spec: OssieSpec):
    shapes = _convert(people_spec, closed=True)
    assert shapes.value(PEOPLE.Employee, SH.closed) == Literal(True)
    # A concept with subtypes stays open: an Employee has properties Person's
    # shape does not list, and a validator that knows Employee is a Person
    # would otherwise reject every Employee.
    assert shapes.value(PEOPLE.Person, SH.closed) == Literal(False)


def test_subtype_shape_carries_the_supertype_properties(people_spec: OssieSpec):
    # The output has no rdfs:subClassOf. Instead, Employee's shape repeats
    # Person's properties, so an Employee is checked for a name too.
    shapes = _convert(people_spec)
    _property_shape(shapes, PEOPLE.Employee, PEOPLE["Employee.earns"])
    name = _property_shape(shapes, PEOPLE.Employee, PEOPLE["Person.name"])
    assert shapes.value(name, SH.minCount) == Literal(1)


def _data(extra: str = "") -> Graph:
    return Graph().parse(format="turtle", data=f"""
        PREFIX p: <https://example.org/people/>
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        p:ann a p:Employee ;
            <https://example.org/people/Person.name> "Ann" ;
            <https://example.org/people/Employee.earns> "1000.0"^^xsd:decimal .
        {extra}
    """)


def test_open_shapes_accept_extra_properties(people_spec: OssieSpec):
    shapes = _convert(people_spec)
    extra = 'p:ann <https://example.org/other/nickname> "Annie" .'
    assert validate(_data(), shacl_graph=shapes)[0]
    assert validate(_data(extra), shacl_graph=shapes)[0]


def test_closed_shapes_reject_only_extra_properties(people_spec: OssieSpec):
    shapes = _convert(people_spec, closed=True)
    extra = 'p:ann <https://example.org/other/nickname> "Annie" .'
    # Valid data still passes: rdf:type is not counted as an extra property.
    assert validate(_data(), shacl_graph=shapes)[0]
    assert not validate(_data(extra), shacl_graph=shapes)[0]


def test_missing_mandatory_property_is_rejected(people_spec: OssieSpec):
    shapes = _convert(people_spec)
    data = Graph().parse(format="turtle", data="""
        PREFIX p: <https://example.org/people/>
        p:bob a p:Person .
    """)
    assert not validate(data, shacl_graph=shapes)[0]


def test_n_triples(people_spec: OssieSpec):
    text = OssieToShaclConverter.convert_spec(
        people_spec, schema_id="https://example.org/people", output_format="nt"
    )
    assert "PREFIX" not in text
    assert Graph().parse(data=text, format="nt").isomorphic(_convert(people_spec))


def test_convert_and_convert_spec_agree(flights_model: OssieOntology):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from_spec = OssieToShaclConverter.convert_spec(
            OssieToSpecConverter.convert(flights_model),
            schema_id="https://example.org/flights",
        )
    assert from_spec == _flights(flights_model)


def test_schema_id_defaults_to_a_placeholder(people_spec: OssieSpec):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        shapes = Graph().parse(
            data=OssieToShaclConverter.convert_spec(people_spec), format="turtle"
        )
    shape_iris = {str(s) for s in shapes.subjects(RDF.type, SH.NodeShape)}
    assert len(shape_iris) == 2
    assert all("People" in iri or "people" in iri for iri in shape_iris)
