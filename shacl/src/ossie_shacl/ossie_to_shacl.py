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


"""Converter from OssieOntology (runtime semantic model) to SHACL shapes.

There is no SHACL code here. The ontology goes through LinkML, with both steps
done by `ohi_linkml` – the `neverblink-linkml` distribution, a natively
compiled LinkML implementation: Ossie -> LinkML (`from_ossie`), then
LinkML -> SHACL (`Schema.shacl`). The LinkML package of this repository does
the first step too, but it is short enough to repeat here rather than depend
on that package."""

from __future__ import annotations

import warnings
from typing import Literal

import ohi_linkml

from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_ontology.model import OssieOntology
from ossie_ontology.spec import OssieSpec


class OssieToShaclConverter:
    """Converts an Ossie ontology into SHACL shapes, returned as RDF text.

    Every entity type becomes a `sh:NodeShape` targeting its class, and each of
    its relationships a property shape with the counts, datatype or class, and
    value ranges that LinkML could carry over.

    Only the ontology crosses over, and only what LinkML can hold: datasets,
    join paths and metrics (`ontology_mappings`) are dropped, and so are
    `derived_by` rules and the expressions LinkML cannot express. Everything
    that is dropped is reported with `warnings.warn` (one `UserWarning` per
    kind). The mapping is documented at
    https://linkml.ohi.dev/docs/importers/ossie/ and
    https://linkml.ohi.dev/docs/generators/shacl/

    Shapes are open by default: data may have properties beyond those in the
    ontology. Pass `closed=True` to reject them.

        shapes_ttl = OssieToShaclConverter.convert(model)
    """

    @staticmethod
    def convert(
        model: OssieOntology,
        schema_id: str | None = None,
        output_format: Literal["ttl", "nt"] = "ttl",
        closed: bool = False,
    ) -> str:
        """Convert a runtime model.

        *schema_id* is the base IRI of the shapes, classes and properties. An
        Ossie ontology carries no IRI of its own, so leaving it unset yields a
        placeholder built from the ontology's name. *output_format* is `ttl`
        (Turtle) or `nt` (N-Triples). *closed* makes every shape closed.
        """
        return OssieToShaclConverter._convert_spec(
            OssieToSpecConverter.convert(model), schema_id, output_format, closed
        )

    @staticmethod
    def convert_spec(
        spec: OssieSpec,
        schema_id: str | None = None,
        output_format: Literal["ttl", "nt"] = "ttl",
        closed: bool = False,
    ) -> str:
        """Convert a spec DTO, skipping the runtime model.

        The shorter path when a document was read straight off disk and never
        needed to be built out into an OssieOntology.
        """
        return OssieToShaclConverter._convert_spec(spec, schema_id, output_format, closed)

    @staticmethod
    def _convert_spec(
        spec: OssieSpec,
        schema_id: str | None,
        output_format: Literal["ttl", "nt"],
        closed: bool,
    ) -> str:
        # Called straight from both public methods, so stacklevel=3 always points
        # the warnings at the caller's code rather than at this module.
        result = ohi_linkml.from_ossie(spec.dump_yaml(), schema_id=schema_id)
        # One line per kind of thing LinkML has no place for, e.g. `derived_by`.
        for warning in result["warnings"]:
            warnings.warn(warning, UserWarning, stacklevel=3)
        # The schema's own lint warnings (no tree_root class, missing
        # descriptions, ...) are left out: they are about the LinkML in the
        # middle, which the caller never sees. A schema that fails to load
        # raises SchemaLoadError.
        with ohi_linkml.load_string(result["schema"]) as schema:
            return schema.shacl(format=output_format, open=not closed)
