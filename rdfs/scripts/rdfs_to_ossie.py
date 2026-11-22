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
#   This script converts an RDFS vocabulary into an Ossie-compliant YAML
#   representation of the ontology it describes, printed to stdout. The input
#   is a Turtle or N-Triples file.
#
#   Every rdfs:Class becomes an entity type and every rdf:Property a
#   relationship of the class in its rdfs:domain; a property with no domain is
#   dropped. The full mapping and all limitations are documented at
#   https://linkml.ohi.dev/docs/importers/owl/ and
#   https://linkml.ohi.dev/docs/generators/ossie/
#
# Usage:
#
#   $ python rdfs_to_ossie.py [--name NAME] <path_to_vocabulary>
#
#   Without --name, the ontology is named after the vocabulary's rdfs:label if
#   that is a usable name, else the prefix of most of its terms.
#
# Outputs:
#
#   - stdout: The Ossie document, as YAML
#   - stderr: One warning per kind of thing that was not imported, such as
#     individuals
#
import argparse
from pathlib import Path

from ossie_rdfs import RdfsToOssieConverter

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert an RDFS vocabulary into an Ossie ontology.")
    parser.add_argument("path", type=Path, help="RDFS vocabulary (Turtle or N-Triples)")
    parser.add_argument("--name", help="name of the ontology")
    args = parser.parse_args()

    document = args.path.read_text(encoding="utf-8")

    print(RdfsToOssieConverter().convert_to_spec(document, name=args.name).dump_yaml())
