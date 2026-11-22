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
#   This script converts an Ossie ontology into an OWL 2 ontology, printed to
#   stdout as Turtle or N-Triples. The input is a single Ossie document, as
#   YAML or JSON.
#
#   The conversion goes through LinkML. Multiplicities become cardinality
#   restrictions, `identify_by` becomes owl:hasKey, a value type that lists
#   its allowed values becomes a class defined by owl:oneOf, and any other
#   value type is replaced by the XSD datatype it is built on, restricted by
#   its value ranges. 'ontology_mappings' (datasets, join paths, metrics) and
#   'derived_by' rules are lost. The full mapping and all limitations are
#   documented at
#   https://linkml.ohi.dev/docs/importers/ossie/ and
#   https://linkml.ohi.dev/docs/generators/owl/
#
# Usage:
#
#   $ python ossie_to_owl.py [--format ttl|nt] <path_to_ossie_document> [ontology_iri]
#
#   An Ossie ontology carries no IRI of its own, so the ontology, its classes
#   and its properties get a placeholder built from the ontology's name unless
#   one is given here.
#
# Outputs:
#
#   - stdout: The OWL ontology, as Turtle (default) or N-Triples
#   - stderr: One warning per kind of thing that was dropped on the way to
#     LinkML, such as `derived_by` rules or ontology mappings. What is dropped
#     on the way from LinkML to OWL, the names of value types, is not reported.
#
import argparse
from pathlib import Path

from ossie_ontology.parser import OssieParser

from ossie_owl import OssieToOwlConverter

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert an Ossie ontology into an OWL ontology.")
    parser.add_argument("path", type=Path, help="Ossie document (.yaml or .json)")
    parser.add_argument("ontology_iri", nargs="?", help="IRI of the ontology, and base IRI of its classes and properties")
    parser.add_argument("--format", choices=["ttl", "nt"], default="ttl", help="RDF serialization of the output (default: ttl)")
    args = parser.parse_args()

    model = OssieParser().parse(args.path)

    print(OssieToOwlConverter.convert(model, schema_id=args.ontology_iri, output_format=args.format))
