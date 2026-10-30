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

"""LinkML <-> Apache Ossie.

`LinkmlToOssieConverter` turns a LinkML schema into an `OssieOntology` from the
`apache-ossie-ontology` package, and `OssieToLinkmlConverter` goes the other
way. The translation itself is done by `linkml_scala`, from the
`neverblink-linkml` distribution.
"""

from ossie_linkml.linkml_to_ossie import LinkmlToOssieConverter
from ossie_linkml.ossie_to_linkml import OssieToLinkmlConverter

__all__ = [
    "LinkmlToOssieConverter",
    "OssieToLinkmlConverter",
]
