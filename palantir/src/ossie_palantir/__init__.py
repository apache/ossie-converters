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

"""Palantir ontology export -> Apache Ossie.

`PalantirParser` reads an export (a `.zip` or an extracted folder) into the
Palantir model in `ossie_palantir.model`, and `PalantirToOssieConverter` turns
that into an `OssieOntology` from the `apache-ossie-ontology` package.
"""

from ossie_palantir.palantir_to_ossie import PalantirToOssieConverter
from ossie_palantir.parser import PalantirParser

__all__ = [
    "PalantirParser",
    "PalantirToOssieConverter",
]
