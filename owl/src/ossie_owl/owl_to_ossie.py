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


"""Converter from an OWL ontology to OssieOntology (runtime semantic model).

`ohi_linkml` – the `neverblink-linkml` distribution – reads the ontology into
a LinkML schema (`from_owl`) and emits an Ossie document from that
(`Schema.ossie`). This module feeds the document through the existing
spec -> model conversion from `apache-ossie-ontology`, so the result is the
same OssieOntology every other Ossie converter produces.

Pairs with ossie_to_owl.OssieToOwlConverter for the other direction."""

from __future__ import annotations

import warnings
from pathlib import Path

import ohi_linkml

from ossie_ontology.converter.spec_to_ossie.converter import SpecToOssieConverter
from ossie_ontology.model import FormulaFactory, MappingFormulaFactory, OssieOntology
from ossie_ontology.spec import OssieSpec


class OwlToOssieConverter:
    """Converts an OWL 2 ontology, as Turtle or N-Triples, to OssieOntology.

    Every class becomes an entity type, with `extends` from its named
    superclasses. Every object or datatype property becomes a relationship of
    the class in its `rdfs:domain`, with its `rdfs:range` as the role's
    concept. A functional property, or a `owl:maxCardinality 1` on the class,
    makes the relationship `ManyToOne`, and a minimum cardinality makes it
    required (`requires` on the concept). `owl:hasKey` becomes `identify_by`,
    a class defined by `owl:oneOf` a value type that lists its values, and
    datatype restrictions such as `xsd:decimal[>= 0]` value ranges. Labels and
    comments fill the verbalizations and descriptions.

    The conversion has two steps, and only losses in the first are reported.
    OWL -> LinkML reports what it does not read, such as individuals outside
    an enumeration, property chains and general class axioms, with
    `warnings.warn` (one `UserWarning` per kind). LinkML -> Ossie then drops,
    without any warning, every property with no `rdfs:domain`, since an Ossie
    relationship belongs to a concept. Each `owl:imports` is not followed: only
    the terms of the ontology given are converted. The mapping is documented at
    https://linkml.ohi.dev/docs/importers/owl/ and
    https://linkml.ohi.dev/docs/generators/ossie/

    Takes the same *formula_factory* / *mapping_formula_factory* as
    SpecToOssieConverter, because it hands the spec -> model step to it. Left
    unset, SpecToOssieConverter's defaults apply and every formula is parsed.

        model = OwlToOssieConverter().convert_file("ontology.ttl")
    """

    def __init__(self, formula_factory: FormulaFactory | None = None,
                 mapping_formula_factory: MappingFormulaFactory | None = None):
        # Left as given so SpecToOssieConverter applies its own default (the
        # parsing factories).
        self._formula_factory = formula_factory
        self._mapping_formula_factory = mapping_formula_factory

    def convert_text(
        self,
        document: str,
        name: str | None = None,
        metadata_language: str = "en",
    ) -> OssieOntology:
        """Convert an ontology held in memory, as Turtle or N-Triples.

        *name* names the ontology. By default it is the ontology's
        `rdfs:label` if that is a usable name, else the prefix of most of its
        terms. *metadata_language* picks the language of labels and comments in
        ontologies that carry translations.
        """
        return self._to_model(self._to_spec(document, name, metadata_language))

    def convert_file(
        self,
        path: str | Path,
        name: str | None = None,
        metadata_language: str = "en",
    ) -> OssieOntology:
        """Read an ontology from disk, as Turtle or N-Triples, and convert it.
        The options are those of `convert_text`."""
        document = Path(path).read_text(encoding="utf-8")
        return self._to_model(self._to_spec(document, name, metadata_language))

    def convert_to_spec(
        self,
        document: str,
        name: str | None = None,
        metadata_language: str = "en",
    ) -> OssieSpec:
        """Convert an ontology, stopping at the spec DTO.

        The shorter path when the caller only wants to write the Ossie document
        out rather than build the runtime model.
        """
        return self._to_spec(document, name, metadata_language)

    def _to_model(self, spec: OssieSpec) -> OssieOntology:
        return SpecToOssieConverter(
            formula_factory=self._formula_factory,
            mapping_formula_factory=self._mapping_formula_factory,
        ).convert(spec)

    @staticmethod
    def _to_spec(document: str, name: str | None, metadata_language: str) -> OssieSpec:
        # Called straight from every public method, so stacklevel=3 always
        # points the warnings at the caller's code rather than at this module.
        # No input format is passed: ohi's Turtle reader also reads N-Triples.
        result = ohi_linkml.from_owl(document, config={"name": name} if name else None)
        # One line per kind of thing the import does not read, e.g. individuals.
        for warning in result["warnings"]:
            warnings.warn(warning, UserWarning, stacklevel=3)
        with ohi_linkml.load_string(result["schema"]) as schema:
            # "schema" keeps what the ontology defines, and leaves out the
            # LinkML built-in types it imports (Int8, Float16, ...).
            return OssieSpec.load_yaml(schema.ossie(
                pruning_mode="schema",
                format="yaml",
                metadata_language=metadata_language,
            ))
