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

"""The curation overlay: everything a CSN document does not carry.

CSN is a complete description of *structure* — entities, keys, associations,
types, fixed values. It is not a description of *language*: it has no
business-facing relationship names, no verbalization sentences, and no binding
to the physical datasets an ontology is populated from. Those are the inputs the
overlay supplies.

The overlay is optional. Without one the emitter produces a structurally
complete ontology with mechanically derived names and template verbalizations;
with one it produces the curated model. Keeping the two separate is what makes
the conversion auditable: the README's mapping tables state, construct by
construct, which side of the line each piece of the output came from.

Overlay shape (all sections optional)::

    model:
      name: sap_business_partner_slice
      description: "..."
    value_types:
      <derived value type name>:
        rename: <final name>
        description: "..."             # `null` suppresses the derived description
        requires: [ "<expression>" ]   # replaces the derived constraints; [] clears them
    concepts:
      <CSN entity simple name>:
        description: "..."
        relationships:
          <CSN element name>:
            rename: <final name>
            description: "..."         # `null` suppresses the derived description
            verbalizes: [ "..." ]
    ontology_mappings:
      - name: ...
        description: ...
        semantic_model: { name, description, datasets: [...] }
        concept_bindings:
          - concept: <ontology concept name>
            dataset: <dataset name>
            identity:   { <CSN element name>: <FIELD> }
            attributes: { <CSN element name>: <FIELD> }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_SENTINEL = object()

_RELATIONSHIP_KEYS = frozenset({"rename", "description", "verbalizes"})
_VALUE_TYPE_KEYS = frozenset({"rename", "description", "requires"})
_TOP_LEVEL_KEYS = frozenset({"model", "value_types", "concepts", "ontology_mappings"})


class CurationError(ValueError):
    """Raised when an overlay is malformed or refers to nothing in the CSN."""


def _description(entry: dict[str, Any], derived: str | None) -> str | None:
    """Apply a curated description.

    An absent key keeps the derived description; an explicit ``null`` suppresses
    it, which is how a curated model drops a description that adds nothing the
    verbalization does not already say.
    """
    return entry.get("description", derived)


@dataclass
class Curation:
    """A parsed curation overlay."""

    model: dict[str, Any] = field(default_factory=dict)
    value_types: dict[str, dict[str, Any]] = field(default_factory=dict)
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    ontology_mappings: list[dict[str, Any]] = field(default_factory=list)
    #: Overlay keys touched during emission; used by :meth:`unused_keys`.
    _touched: set[str] = field(default_factory=set, repr=False)

    # -- value types ------------------------------------------------------

    def value_type(self, derived_name: str) -> dict[str, Any]:
        entry = self.value_types.get(derived_name)
        if entry is not None:
            self._touched.add(f"value_types.{derived_name}")
        return entry or {}

    def value_type_name(self, derived_name: str) -> str:
        return self.value_type(derived_name).get("rename", derived_name)

    def value_type_requires(self, derived_name: str, derived: list[str]) -> list[str]:
        """Curated constraints replace derived ones; an empty list clears them."""
        override = self.value_type(derived_name).get("requires", _SENTINEL)
        return derived if override is _SENTINEL else list(override or [])

    def value_type_description(self, derived_name: str, derived: str | None) -> str | None:
        return _description(self.value_type(derived_name), derived)

    # -- concepts and relationships ---------------------------------------

    def concept(self, entity_simple_name: str) -> dict[str, Any]:
        entry = self.concepts.get(entity_simple_name)
        if entry is not None:
            self._touched.add(f"concepts.{entity_simple_name}")
        return entry or {}

    def relationship(self, entity_simple_name: str, csn_element_name: str) -> dict[str, Any]:
        entry = (self.concept(entity_simple_name).get("relationships") or {}).get(csn_element_name)
        if entry is not None:
            self._touched.add(f"concepts.{entity_simple_name}.relationships.{csn_element_name}")
        return entry or {}

    def concept_description(self, entity_simple_name: str, derived: str | None) -> str | None:
        return _description(self.concept(entity_simple_name), derived)

    def relationship_description(
        self, entity_simple_name: str, csn_element_name: str, derived: str | None
    ) -> str | None:
        return _description(self.relationship(entity_simple_name, csn_element_name), derived)

    # -- integrity --------------------------------------------------------

    def unused_keys(self) -> list[str]:
        """Overlay entries that matched nothing in the CSN.

        An unused key means the overlay has drifted from its source model — the
        element was renamed or removed upstream — so callers surface it rather
        than letting curation rot silently.
        """
        declared: list[str] = [f"value_types.{name}" for name in self.value_types]
        for concept_name, entry in self.concepts.items():
            declared.append(f"concepts.{concept_name}")
            for element_name in (entry.get("relationships") or {}):
                declared.append(f"concepts.{concept_name}.relationships.{element_name}")
        return sorted(key for key in declared if key not in self._touched)


def _check_keys(where: str, entry: Any, allowed: frozenset[str]) -> None:
    if not isinstance(entry, dict):
        raise CurationError(f"{where}: expected a mapping, got {type(entry).__name__}")
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise CurationError(f"{where}: unknown key(s) {unknown}; allowed keys are {sorted(allowed)}")


def parse_curation(document: dict[str, Any] | None) -> Curation:
    """Validate and parse a curation overlay document."""
    if not document:
        return Curation()
    _check_keys("curation", document, _TOP_LEVEL_KEYS)

    for name, entry in (document.get("value_types") or {}).items():
        _check_keys(f"value_types.{name}", entry, _VALUE_TYPE_KEYS)
    for concept_name, entry in (document.get("concepts") or {}).items():
        _check_keys(f"concepts.{concept_name}", entry, frozenset({"description", "relationships"}))
        for element_name, rel in (entry.get("relationships") or {}).items():
            _check_keys(
                f"concepts.{concept_name}.relationships.{element_name}", rel, _RELATIONSHIP_KEYS
            )

    return Curation(
        model=dict(document.get("model") or {}),
        value_types={k: dict(v) for k, v in (document.get("value_types") or {}).items()},
        concepts={k: dict(v) for k, v in (document.get("concepts") or {}).items()},
        ontology_mappings=[dict(m) for m in (document.get("ontology_mappings") or [])],
    )


def load_curation(path: str | Path | None) -> Curation:
    """Load a curation overlay from disk; ``None`` yields an empty overlay."""
    if path is None:
        return Curation()
    with open(path, encoding="utf-8") as handle:
        return parse_curation(yaml.safe_load(handle))
