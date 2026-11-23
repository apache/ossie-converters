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


"""Converter from OssieOntology (runtime semantic model) to RDFS.

There is no RDFS code here. The ontology goes through LinkML, with both steps
done by `ohi_linkml` – the `neverblink-linkml` distribution, a natively
compiled LinkML implementation: Ossie -> LinkML (`load_ossie`), then
LinkML -> RDFS (`Schema.rdfs`).

Pairs with rdfs_to_ossie.RdfsToOssieConverter for the other direction."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Literal

import ohi_linkml

from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_ontology.model import OssieOntology
from ossie_ontology.parser import OssieParser
from ossie_ontology.spec import OssieSpec


class OssieToRdfsConverter:
    """Converts an Ossie ontology into an RDFS vocabulary, returned as RDF text.

    Every entity type becomes an `rdfs:Class`, with `rdfs:subClassOf` for the
    concepts it extends, and each of its relationships an `rdf:Property` with
    `rdfs:domain` and `rdfs:range`. A value type that lists its allowed values
    becomes a class too, with the values as its instances. Any other value
    type is not in the output: a relationship to it gets the XSD datatype it
    is built on as its range.

    The conversion has two steps, and only losses in the first are reported.
    Ossie -> LinkML drops datasets, join paths and metrics
    (`ontology_mappings`), `derived_by` rules and expressions LinkML cannot
    express, and reports each kind with `warnings.warn` (one `UserWarning`).
    LinkML -> RDFS then drops, without any warning, everything RDFS cannot
    say: multiplicities, the names of value types other than enumerations,
    and their `requires`, such as value ranges. The mapping is documented at
    https://linkml.ohi.dev/docs/importers/ossie/ and
    https://linkml.ohi.dev/docs/generators/rdfs/

        vocabulary_ttl = OssieToRdfsConverter.convert(model)
        vocabulary_ttl = OssieToRdfsConverter.convert_file("ontology.yaml")
    """

    @staticmethod
    def convert(
        model: OssieOntology,
        schema_id: str | None = None,
        output_format: Literal["ttl", "nt"] = "ttl",
    ) -> str:
        """Convert a runtime model.

        *schema_id* is the base IRI of the classes and properties. An Ossie
        ontology carries no IRI of its own, so leaving it unset yields a
        placeholder built from the ontology's name. *output_format* is `ttl`
        (Turtle) or `nt` (N-Triples).
        """
        return OssieToRdfsConverter._convert_spec(
            OssieToSpecConverter.convert(model), schema_id, output_format
        )

    @staticmethod
    def convert_spec(
        spec: OssieSpec,
        schema_id: str | None = None,
        output_format: Literal["ttl", "nt"] = "ttl",
    ) -> str:
        """Convert a spec DTO, skipping the runtime model.

        The shorter path when a document was read straight off disk and never
        needed to be built out into an OssieOntology.
        """
        return OssieToRdfsConverter._convert_spec(spec, schema_id, output_format)

    @staticmethod
    def convert_file(
        path: str | Path,
        schema_id: str | None = None,
        output_format: Literal["ttl", "nt"] = "ttl",
    ) -> str:
        """Read an Ossie document from disk, as YAML or JSON, and convert it.
        The options are those of `convert`."""
        model = OssieParser().parse(Path(path))
        return OssieToRdfsConverter._convert_spec(
            OssieToSpecConverter.convert(model), schema_id, output_format
        )

    @staticmethod
    def _convert_spec(
        spec: OssieSpec,
        schema_id: str | None,
        output_format: Literal["ttl", "nt"],
    ) -> str:
        # Called straight from every public method, so stacklevel=3 always points
        # the warnings at the caller's code rather than at this module.
        # The schema's own lint warnings (no tree_root class, missing
        # descriptions, ...) are left out: they are about the LinkML in the
        # middle, which the caller never sees. A document that cannot be
        # imported raises SchemaConversionError.
        with ohi_linkml.load_ossie(spec.dump_yaml(), schema_id=schema_id) as schema:
            # One line per kind of thing LinkML has no place for, e.g. `derived_by`.
            for warning in schema.not_imported["warnings"]:
                warnings.warn(warning, UserWarning, stacklevel=3)
            return schema.rdfs(format=output_format)
