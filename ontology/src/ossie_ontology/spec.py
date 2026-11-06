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
"""Deprecated: import the ontology DTOs from `ossie.models` instead.

The DTOs that used to live here are now the shared models of the `apache-ossie`
package. This module only re-exports them under their old names, so open work
keeps importing during the transition. Every name warns with its replacement.

Only the names carry over; the behaviour is that of `ossie.models`. The models
are frozen, absent lists are `None`, and `OssieSpec.load_yaml` / `dump_yaml`
become `OssieOntologyModel.from_ossie_yaml` / `to_ossie_yaml`.

The module is removed once nothing in progress imports it.
"""

from __future__ import annotations

import warnings
from typing import Any

import ossie.models

# Old name -> its replacement in `ossie.models`.
_RENAMED = {
    "OssieSpec": "OssieOntologyModel",
    "ConceptComponent": "OssieConceptComponent",
    "Relationship": "OssieOntologyRelationship",
    "Role": "OssieRole",
    "OntologyMapping": "OssieOntologyMapping",
    "ConceptMapping": "OssieConceptMapping",
    "ObjectMapping": "OssieObjectMapping",
    "ReferentMapping": "OssieReferentMapping",
    "LinkMapping": "OssieLinkMapping",
    "SemanticModel": "OssieSemanticModel",
    "Dataset": "OssieDataset",
    "DatasetField": "OssieField",
    "Metric": "OssieMetric",
    "Dimension": "OssieDimension",
    "JoinPath": "OssieRelationship",
    "Expression": "OssieExpression",
    "DialectExpression": "OssieDialectExpression",
    "CustomExtension": "OssieCustomExtension",
    "AiContext": "OssieAIContext",
    "DataType": "OssieDataType",
    "CoreVersion": "OssieVersion",
}

__all__ = list(_RENAMED)


def __getattr__(name: str) -> Any:
    # Resolved lazily (PEP 562) so each use of an old name warns, including
    # `from ossie_ontology.spec import OssieSpec`.
    new_name = _RENAMED.get(name)
    if new_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"ossie_ontology.spec.{name} is deprecated; "
        f"use ossie.models.{new_name} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(ossie.models, new_name)


def __dir__() -> list[str]:
    return sorted([*globals(), *_RENAMED])
