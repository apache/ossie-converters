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
#   This script converts an OWL 2 ontology into an Ossie-compliant YAML
#   representation of it, printed to stdout. The input is a Turtle or
#   N-Triples file.
#
#   Every class becomes an entity type and every object or datatype property a
#   relationship of the class in its rdfs:domain; a property with no domain is
#   dropped. owl:imports are not followed. The full mapping and all
#   limitations are documented at
#   https://linkml.ohi.dev/docs/importers/owl/ and
#   https://linkml.ohi.dev/docs/generators/ossie/
#
# Usage:
#
#   $ python owl_to_ossie.py [--name NAME] [--metadata-language LANG] [--raw-formulas] <path_to_ontology>
#
#   Without --name, the ontology is named after its rdfs:label if that is a
#   usable name, else the prefix of most of its terms. --metadata-language
#   picks the language of labels and comments in ontologies that carry
#   translations (default: en).
#
#   The document is built into the Ossie runtime model before it is printed,
#   so output that is not valid Ossie stops the run with an error. That also
#   parses every formula; --raw-formulas skips parsing them, for ontologies
#   with formulas the parser cannot read yet, such as the REGEXP_LIKE that a
#   pattern (xsd:pattern) becomes.
#
# Outputs:
#
#   - stdout: The Ossie document, as YAML
#   - stderr: One warning per kind of thing the OWL -> LinkML step did not
#     read, such as individuals outside an enumeration. Properties with no
#     rdfs:domain are dropped on the way from LinkML to Ossie without a
#     warning.
#
import argparse
from pathlib import Path

from ossie_ontology.converter.spec_to_ossie.converter import SpecToOssieConverter
from ossie_ontology.model import FormulaFactory

from ossie_owl import OwlToOssieConverter

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert an OWL ontology into an Ossie ontology.")
    parser.add_argument("path", type=Path, help="OWL ontology (Turtle or N-Triples)")
    parser.add_argument("--name", help="name of the ontology")
    parser.add_argument("--metadata-language", default="en", help="language of labels and comments (default: en)")
    parser.add_argument("--raw-formulas", action="store_true", help="do not parse formulas when checking the output")
    args = parser.parse_args()

    document = args.path.read_text(encoding="utf-8")
    spec = OwlToOssieConverter().convert_to_spec(
        document, name=args.name, metadata_language=args.metadata_language
    )
    # Build the runtime model only as a check: it raises if the document is
    # not valid Ossie. The document printed is the one ohi wrote.
    SpecToOssieConverter(formula_factory=FormulaFactory() if args.raw_formulas else None).convert(spec)

    print(spec.dump_yaml())
