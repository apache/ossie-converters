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


"""Tests for OssieToOwlConverter and OwlToOssieConverter.

The OWL and the Ossie documents are made by ohi LinkML, so these tests do not
pin their exact text. They check the parts this package is responsible for
(options, warnings, the entry points, the spec -> model step) and the shape
of the mapping in both directions, reading RDF back with rdflib."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_ontology.model import FormulaFactory, OssieOntology
from ossie_ontology.spec import OssieSpec

from ossie_owl import OssieToOwlConverter, OwlToOssieConverter

FLIGHTS = Namespace("https://example.org/flights/")
PEOPLE = Namespace("https://example.org/people/")
# Not rdflib's XSD namespace: pytest pokes at module attributes while
# collecting, and that namespace warns on every unknown one.
XSD = "http://www.w3.org/2001/XMLSchema#"
XSD_DECIMAL = URIRef(XSD + "decimal")
XSD_MIN_INCLUSIVE = URIRef(XSD + "minInclusive")
XSD_MAX_INCLUSIVE = URIRef(XSD + "maxInclusive")

# A small ontology with a subtype, which drops nothing on the way to OWL.
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

# A small OWL ontology: a subclass, a key, an enumeration, a datatype
# restriction, cardinalities, and a property with no domain.
LIBRARY_OWL = """\
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX lib: <https://example.org/library/>

<https://example.org/library> a owl:Ontology ;
  rdfs:label "Library" .

lib:Agent a owl:Class ;
  rdfs:comment "Someone who can write a book." .
lib:Person a owl:Class ;
  rdfs:subClassOf lib:Agent .
lib:Book a owl:Class ;
  rdfs:subClassOf [
    a owl:Restriction ; owl:onProperty lib:isbn ; owl:minCardinality "1"^^xsd:nonNegativeInteger
  ] , [
    a owl:Restriction ; owl:onProperty lib:author ; owl:maxCardinality "1"^^xsd:nonNegativeInteger
  ] ;
  owl:hasKey ( lib:isbn ) .

lib:Format a owl:Class ;
  owl:equivalentClass [ a owl:Class ; owl:oneOf ( lib:Hardcover lib:Paperback ) ] .
lib:Hardcover a owl:NamedIndividual , lib:Format .
lib:Paperback a owl:NamedIndividual , lib:Format .

lib:isbn a owl:DatatypeProperty , owl:FunctionalProperty ;
  rdfs:label "has ISBN" ;
  rdfs:comment "The ISBN of the book." ;
  rdfs:domain lib:Book ;
  rdfs:range xsd:string .
lib:author a owl:ObjectProperty ;
  rdfs:label "is written by" ;
  rdfs:domain lib:Book ;
  rdfs:range lib:Agent .
lib:pages a owl:DatatypeProperty , owl:FunctionalProperty ;
  rdfs:domain lib:Book ;
  rdfs:range [ a rdfs:Datatype ; owl:onDatatype xsd:integer ;
               owl:withRestrictions ( [ xsd:minInclusive 1 ] ) ] .
lib:format a owl:ObjectProperty , owl:FunctionalProperty ;
  rdfs:domain lib:Book ;
  rdfs:range lib:Format .
lib:related a owl:ObjectProperty .
"""

# A property whose values must match a regular expression.
SHELF_MARK_OWL = """
lib:shelfMark a owl:DatatypeProperty ;
  rdfs:domain lib:Book ;
  rdfs:range [ a rdfs:Datatype ; owl:onDatatype xsd:string ;
               owl:withRestrictions ( [ xsd:pattern "[A-Z]+[0-9]+" ] ) ] .
"""


def _no_warnings(convert, *args, **kwargs):
    """Call *convert*, failing on any warning: only use it for input that
    crosses over in full, such as People and the library ontology."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return convert(*args, **kwargs)


def _people_owl(**options) -> Graph:
    text = _no_warnings(
        OssieToOwlConverter.convert_spec,
        OssieSpec.load_yaml(PEOPLE_OSSIE),
        schema_id="https://example.org/people",
        **options,
    )
    return Graph().parse(data=text, format=options.get("output_format", "turtle"))


def _flights_owl(model: OssieOntology) -> str:
    # Flights has mappings and `derived_by` rules that OWL here cannot hold;
    # the warnings about them are checked in their own test.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return OssieToOwlConverter.convert(model, schema_id="https://example.org/flights")


def _restrictions(graph: Graph, cls: URIRef, prop: URIRef) -> dict:
    """The restrictions *cls* puts on *prop*, as {restriction kind: value}."""
    found = {}
    for restriction in graph.objects(cls, RDFS.subClassOf):
        if graph.value(restriction, OWL.onProperty) != prop:
            continue
        for kind, value in graph.predicate_objects(restriction):
            if kind not in (RDF.type, OWL.onProperty):
                # Counts are typed xsd:nonNegativeInteger; compare the numbers.
                found[kind] = value.toPython() if isinstance(value, Literal) else value
    return found


@pytest.fixture(scope="module")
def flights_owl(flights_model: OssieOntology) -> Graph:
    return Graph().parse(data=_flights_owl(flights_model), format="turtle")


@pytest.fixture(scope="module")
def library_spec() -> OssieSpec:
    return _no_warnings(OwlToOssieConverter().convert_to_spec, LIBRARY_OWL)


# Ossie -> OWL


def test_ontology_and_classes(flights_model: OssieOntology, flights_owl: Graph):
    assert (URIRef("https://example.org/flights"), RDF.type, OWL.Ontology) in flights_owl
    entity_types = {
        c.concept for c in OssieToSpecConverter.convert(flights_model).ontology
        if c.type == "EntityType"
    }
    # Named classes only: the enumeration's `owl:oneOf` class has no IRI.
    classes = {c for c in flights_owl.subjects(RDF.type, OWL.Class) if isinstance(c, URIRef)}
    # CancelationCode is a value type that lists its values: an enumeration.
    assert classes == {FLIGHTS[name] for name in entity_types} | {FLIGHTS.CancelationCode}


def test_relationship_to_an_entity_type(flights_owl: Graph):
    city = FLIGHTS["Airport.city"]
    assert (city, RDF.type, OWL.ObjectProperty) in flights_owl
    assert flights_owl.value(city, RDFS.domain) == FLIGHTS.Airport
    assert flights_owl.value(city, RDFS.range) == FLIGHTS.City
    # ManyToOne: at most one city per airport.
    assert _restrictions(flights_owl, FLIGHTS.Airport, city) == {
        OWL.allValuesFrom: FLIGHTS.City,
        OWL.maxCardinality: 1,
    }


def test_relationship_to_a_value_type_with_a_range(flights_owl: Graph):
    latitude = FLIGHTS["Airport.latitude"]
    assert (latitude, RDF.type, OWL.DatatypeProperty) in flights_owl
    assert (latitude, RDF.type, OWL.FunctionalProperty) in flights_owl
    # `requires: [Airport.latitude]` and ManyToOne: exactly one.
    assert _restrictions(flights_owl, FLIGHTS.Airport, latitude)[OWL.cardinality] == 1
    # DegreesLatitude is not in the output by name; its bounds restrict the
    # datatype it is built on.
    datatype = flights_owl.value(latitude, RDFS.range)
    assert flights_owl.value(datatype, OWL.onDatatype) == XSD_DECIMAL
    facets = {
        kind: value.toPython()
        for item in flights_owl.items(flights_owl.value(datatype, OWL.withRestrictions))
        for kind, value in flights_owl.predicate_objects(item)
    }
    assert facets == {XSD_MIN_INCLUSIVE: -90, XSD_MAX_INCLUSIVE: 90}


def test_identify_by_becomes_a_key(flights_owl: Graph):
    key = flights_owl.value(FLIGHTS.City, OWL.hasKey)
    assert list(flights_owl.items(key)) == [FLIGHTS["City.name"], FLIGHTS["City.state"]]


def test_enumeration(flights_owl: Graph):
    one_of = flights_owl.value(
        flights_owl.value(FLIGHTS.CancelationCode, OWL.equivalentClass), OWL.oneOf
    )
    values = list(flights_owl.items(one_of))
    assert values == [FLIGHTS[f"CancelationCode.{c}"] for c in "ABCD"]
    assert all((v, RDF.type, OWL.NamedIndividual) in flights_owl for v in values)


def test_subtype_becomes_a_subclass():
    owl = _people_owl()
    assert (PEOPLE.Employee, RDFS.subClassOf, PEOPLE.Person) in owl
    # No multiplicity: any number of values, so no cardinality at all.
    assert _restrictions(owl, PEOPLE.Person, PEOPLE["Person.knows"]) == {
        OWL.allValuesFrom: PEOPLE.Person,
    }


def test_what_owl_cannot_hold_is_warned_about(flights_model: OssieOntology):
    with pytest.warns(UserWarning) as record:
        OssieToOwlConverter.convert(flights_model)
    # Only keywords are matched, as ohi may reword its messages in any release.
    messages = [str(w.message).lower() for w in record]
    assert any("mapping" in m for m in messages)
    assert any("derived_by" in m for m in messages)
    # The warnings point at the caller, not at this package.
    assert all(w.filename == __file__ for w in record)


def test_n_triples():
    text = _no_warnings(
        OssieToOwlConverter.convert_spec,
        OssieSpec.load_yaml(PEOPLE_OSSIE),
        schema_id="https://example.org/people",
        output_format="nt",
    )
    assert "PREFIX" not in text
    assert Graph().parse(data=text, format="nt").isomorphic(_people_owl())


def test_convert_file(flights_path: Path, flights_model: OssieOntology):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from_file = OssieToOwlConverter.convert_file(
            flights_path, schema_id="https://example.org/flights"
        )
    assert Graph().parse(data=from_file, format="turtle").isomorphic(
        Graph().parse(data=_flights_owl(flights_model), format="turtle")
    )


def test_convert_and_convert_spec_agree(flights_model: OssieOntology):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from_spec = OssieToOwlConverter.convert_spec(
            OssieToSpecConverter.convert(flights_model),
            schema_id="https://example.org/flights",
        )
    # Compared as graphs: the order of the triples in the text is not fixed.
    assert Graph().parse(data=from_spec, format="turtle").isomorphic(
        Graph().parse(data=_flights_owl(flights_model), format="turtle")
    )


# OWL -> Ossie


def _concept(spec: OssieSpec, name: str):
    return next(c for c in spec.ontology if c.concept == name)


def _relationships(spec: OssieSpec, concept: str) -> dict:
    return {r.name: r for r in _concept(spec, concept).relationships}


def test_classes_become_entity_types(library_spec: OssieSpec):
    assert library_spec.name == "Library"
    assert library_spec.prefixes == {"lib": "https://example.org/library/"}
    assert _concept(library_spec, "Person").extends == ["Agent"]
    assert _concept(library_spec, "Agent").description == "Someone who can write a book."
    assert _concept(library_spec, "Book").iri == "lib:Book"


def test_properties_become_relationships_of_their_domain(library_spec: OssieSpec):
    book = _relationships(library_spec, "Book")
    assert set(book) == {"isbn", "author", "pages", "format"}
    assert {name: [r.concept for r in rel.roles] for name, rel in book.items()} == {
        "isbn": ["String"],
        "author": ["Agent"],
        "pages": ["Integer"],
        "format": ["Format"],
    }
    assert book["author"].verbalizes == ["{Book} is written by {Agent}"]
    assert book["isbn"].description == "The ISBN of the book."


def test_cardinalities_and_keys(library_spec: OssieSpec):
    book = _relationships(library_spec, "Book")
    # A functional property and `owl:maxCardinality 1` both mean one value.
    assert {name: rel.multiplicity for name, rel in book.items()} == {
        "isbn": "ManyToOne",
        "author": "ManyToOne",
        "pages": "ManyToOne",
        "format": "ManyToOne",
    }
    # `owl:minCardinality 1` makes the relationship required.
    assert _concept(library_spec, "Book").requires == ["Book.isbn"]
    assert _concept(library_spec, "Book").identify_by == ["isbn"]


def test_value_restrictions(library_spec: OssieSpec):
    # A datatype restriction becomes a value range on the relationship.
    assert _relationships(library_spec, "Book")["pages"].requires == ["Integer >= 1"]
    # A class defined by `owl:oneOf` becomes a value type that lists its values.
    format_ = _concept(library_spec, "Format")
    assert (format_.type, format_.extends) == ("ValueType", ["String"])
    assert format_.requires == ["Format IN ('Hardcover', 'Paperback')"]


def test_property_without_a_domain_is_dropped(library_spec: OssieSpec):
    # An Ossie relationship belongs to a concept, so `lib:related` has nowhere
    # to go, and this is not reported.
    names = {r.name for c in library_spec.ontology for r in c.relationships}
    assert "related" not in names


def test_what_ossie_cannot_hold_is_warned_about():
    document = LIBRARY_OWL + "lib:mobyDick a lib:Book .\n"
    with pytest.warns(UserWarning) as record:
        OwlToOssieConverter().convert(document)
    assert any("individual" in str(w.message).lower() for w in record)
    assert all(w.filename == __file__ for w in record)


def test_runtime_model_is_built(tmp_path: Path):
    path = tmp_path / "library.ttl"
    path.write_text(LIBRARY_OWL, encoding="utf-8")
    model = _no_warnings(OwlToOssieConverter().convert_file, path)
    assert isinstance(model, OssieOntology)
    assert [c.name for c in model.ontology.lookup_concept("Person").extends] == ["Agent"]
    assert model.ontology.lookup_concept("Format").is_value_type


def test_spec_then_model(library_spec: OssieSpec):
    # The same model as `convert`, built in two steps.
    model = OwlToOssieConverter().to_model(library_spec)
    assert {c.name for c in model.ontology.concepts() if not c.is_builtin} == {
        "Agent", "Person", "Book", "Format",
    }


def test_labels_in_another_language_are_kept():
    # Only Italian text: asking for English still gets it, rather than nothing.
    italian = '"Qualcuno che può scrivere un libro."@it'
    english = '"Someone who can write a book."'
    document = LIBRARY_OWL.replace(english, italian)
    spec = _no_warnings(OwlToOssieConverter().convert_to_spec, document, metadata_language="en")
    assert _concept(spec, "Agent").description == "Qualcuno che può scrivere un libro."
    # With both, the language asked for wins.
    both = LIBRARY_OWL.replace(english, f"{italian} , {english}@en")
    for language, text in [
        ("it", "Qualcuno che può scrivere un libro."),
        ("en", "Someone who can write a book."),
    ]:
        spec = _no_warnings(OwlToOssieConverter().convert_to_spec, both, metadata_language=language)
        assert _concept(spec, "Agent").description == text


def test_pattern_is_not_parsed_yet():
    # A pattern comes over as `REGEXP_LIKE(...)`, which the expression
    # language defines but the ontology package's parser does not read yet.
    # Once it does, this test fails: drop it, and expect a parsed formula.
    document = LIBRARY_OWL + SHELF_MARK_OWL
    shelf_mark = _relationships(
        _no_warnings(OwlToOssieConverter().convert_to_spec, document), "Book"
    )["shelf_mark"]
    assert shelf_mark.requires == ["REGEXP_LIKE(String, '^[A-Z]+[0-9]+$')"]
    with pytest.raises(ValueError, match="REGEXP_LIKE"):
        OwlToOssieConverter().convert(document)
    # Keeping formulas as raw text gets past it.
    _no_warnings(OwlToOssieConverter(formula_factory=FormulaFactory()).convert, document)


def test_n_triples_input(library_spec: OssieSpec):
    n_triples = Graph().parse(data=LIBRARY_OWL, format="turtle").serialize(format="nt")
    from_nt = _no_warnings(OwlToOssieConverter().convert_to_spec, n_triples)
    assert {c.concept for c in from_nt.ontology} == {c.concept for c in library_spec.ontology}


def test_ontology_name():
    spec = _no_warnings(OwlToOssieConverter().convert_to_spec, LIBRARY_OWL, name="Books")
    assert spec.name == "Books"


# Both ways


def test_round_trip_keeps_classes_subtypes_and_counts():
    owl = _no_warnings(
        OssieToOwlConverter.convert_spec,
        OssieSpec.load_yaml(PEOPLE_OSSIE),
        schema_id="https://example.org/people",
    )
    back = _no_warnings(OwlToOssieConverter().convert_to_spec, owl)
    assert {c.concept for c in back.ontology} == {"Person", "Employee"}
    assert _concept(back, "Employee").extends == ["Person"]
    person = _relationships(back, "Person")
    # Value types come back as the built-in they were built on, and the
    # names are made from the property IRIs, which carry the concept.
    assert {name: [r.concept for r in rel.roles] for name, rel in person.items()} == {
        "person_name": ["String"],
        "person_knows": ["Person"],
    }
    # `name` was OneToOne and the key; it comes back required, ManyToOne, and
    # no longer a key.
    assert person["person_name"].multiplicity == "ManyToOne"
    assert _concept(back, "Person").requires == ["Person.person_name"]
    assert not _concept(back, "Person").identify_by
    assert person["person_knows"].multiplicity is None
