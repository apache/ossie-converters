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


"""OWL <-> Apache Ossie.

`OssieToOwlConverter` turns an `OssieOntology` from the
`apache-ossie-ontology` package into an OWL ontology, and
`OwlToOssieConverter` goes the other way. The translation itself is done by
`ohi_linkml`, from the `neverblink-linkml` distribution, by way of LinkML.
"""

from ossie_owl.ossie_to_owl import OssieToOwlConverter
from ossie_owl.owl_to_ossie import OwlToOssieConverter

__all__ = [
    "OssieToOwlConverter",
    "OwlToOssieConverter",
]
