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

# Description:
#
#   This script converts an Ossie ontology into SHACL shapes, printed to stdout
#   as Turtle or N-Triples. The input is a single Ossie document, as YAML or
#   JSON.
#
#   The conversion goes through LinkML. Only the ontology is converted:
#   'ontology_mappings' (datasets, join paths, metrics) and 'derived_by' rules
#   are dropped. The full mapping and all limitations are documented at
#   https://linkml.ohi.dev/docs/importers/ossie/ and
#   https://linkml.ohi.dev/docs/generators/shacl/
#
# Usage:
#
#   $ python ossie_to_shacl.py [--closed] [--format ttl|nt] <path_to_ossie_document> [base_iri]
#
#   Shapes are open (they allow properties beyond the ontology's) unless
#   --closed is given. An Ossie ontology carries no IRI of its own, so the
#   shapes get a placeholder built from the ontology's name unless one is
#   given here.
#
# Outputs:
#
#   - stdout: The SHACL shapes, as Turtle (default) or N-Triples
#   - stderr: One warning per kind of thing that was dropped, such as
#     `derived_by` rules or ontology mappings
#
import argparse
from pathlib import Path

from ossie_ontology.parser import OssieParser

from ossie_shacl import OssieToShaclConverter

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert an Ossie ontology into SHACL shapes.")
    parser.add_argument("path", type=Path, help="Ossie document (.yaml or .json)")
    parser.add_argument("base_iri", nargs="?", help="base IRI of the shapes, classes and properties")
    parser.add_argument("--closed", action="store_true", help="reject properties the ontology does not declare")
    parser.add_argument("--format", choices=["ttl", "nt"], default="ttl", help="RDF serialization of the output (default: ttl)")
    args = parser.parse_args()

    model = OssieParser().parse(args.path)

    print(OssieToShaclConverter.convert(
        model, schema_id=args.base_iri, output_format=args.format, closed=args.closed
    ))
