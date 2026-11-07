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

"""Reader for CAP-style CSN (Core Schema Notation) documents.

This module is deliberately structural: it normalizes the parts of CSN the
ontology emitter reasons about (definitions, elements, keys, associations and
their ``ON`` conditions, fixed-value enumerations, labels) and keeps everything
else in ``annotations`` so nothing is silently dropped before the mapping rules
get a chance to look at it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: CSN scalar types to Apache Ossie built-in value types (``extends``).
CDS_TO_OSSIE_BUILTIN = {
    "cds.String": "String",
    "cds.LargeString": "String",
    "cds.Binary": "Opaque",
    "cds.LargeBinary": "Opaque",
    "cds.Boolean": "Boolean",
    "cds.UUID": "String",
    "cds.Integer": "Integer",
    "cds.Integer64": "Integer",
    "cds.UInt8": "Integer",
    "cds.Int16": "Integer",
    "cds.Int32": "Integer",
    "cds.Int64": "Integer",
    "cds.Decimal": "Decimal",
    "cds.DecimalFloat": "Decimal",
    "cds.Double": "Float",
    "cds.Date": "Date",
    "cds.Time": "Time",
    "cds.DateTime": "DateTime",
    "cds.Timestamp": "DateTimeTz",
}

ASSOCIATION_TYPES = frozenset({"cds.Association", "cds.Composition"})

#: CSN annotation carrying the end-user label. SAP models use this on both
#: definitions and elements; it is the raw material for verbalizations.
LABEL_ANNOTATION = "@EndUserText.label"


@dataclass(frozen=True)
class JoinCondition:
    """One ``left = right`` equality parsed out of an association ``on`` clause.

    ``remote`` is the path rooted at the association name (e.g.
    ``["_Customer", "BusinessPartner"]``); ``local`` is the path in the source
    entity (e.g. ``["BusinessPartner"]``).
    """

    remote: tuple[str, ...]
    local: tuple[str, ...]

    @property
    def remote_element(self) -> str:
        return self.remote[-1]

    @property
    def local_element(self) -> str:
        return self.local[-1]


@dataclass
class Element:
    """One element of a CSN entity."""

    name: str
    type: str | None = None
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_key: bool = False
    label: str | None = None
    comment: str | None = None
    enum: dict[str, Any] = field(default_factory=dict)
    target: str | None = None
    cardinality: dict[str, Any] = field(default_factory=dict)
    on: list[JoinCondition] = field(default_factory=list)
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def is_association(self) -> bool:
        return self.type in ASSOCIATION_TYPES

    @property
    def is_to_many(self) -> bool:
        """A CSN association is to-many when its max cardinality is not 1."""
        if not self.is_association:
            return False
        return str(self.cardinality.get("max", 1)) != "1"

    @property
    def builtin(self) -> str | None:
        """The Apache Ossie built-in this element's scalar type extends, if any."""
        return CDS_TO_OSSIE_BUILTIN.get(self.type or "")


@dataclass
class Definition:
    """One CSN definition: an entity, a named type, or an aspect."""

    name: str
    kind: str
    label: str | None = None
    comment: str | None = None
    type: str | None = None
    length: int | None = None
    enum: dict[str, Any] = field(default_factory=dict)
    elements: dict[str, Element] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def simple_name(self) -> str:
        """The definition name without its CSN namespace prefix."""
        return self.name.rsplit(".", 1)[-1]

    @property
    def key_elements(self) -> list[Element]:
        return [e for e in self.elements.values() if e.is_key]

    @property
    def builtin(self) -> str | None:
        return CDS_TO_OSSIE_BUILTIN.get(self.type or "")


@dataclass
class CsnModel:
    """A parsed CSN document."""

    definitions: dict[str, Definition] = field(default_factory=dict)
    csn_interop_effective: str | None = None
    comment: str | None = None

    def entities(self) -> list[Definition]:
        return [d for d in self.definitions.values() if d.kind == "entity"]

    def resolve(self, type_name: str | None) -> Definition | None:
        """Resolve a CSN type reference to its definition, if it is a named type."""
        if not type_name:
            return None
        return self.definitions.get(type_name)

    def entity_by_simple_name(self, simple_name: str) -> Definition | None:
        for d in self.definitions.values():
            if d.kind == "entity" and d.simple_name == simple_name:
                return d
        return None


def _annotations(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k.startswith("@")}


def _parse_on(raw_on: Any, association_name: str) -> list[JoinCondition]:
    """Parse a CSN ``on`` clause into equality conditions.

    CSN encodes ``on`` as a flat token list, e.g.::

        [{"ref": ["_Customer", "BusinessPartner"]}, "=", {"ref": ["BusinessPartner"]}]

    Only ``=`` comparisons are interpreted; anything else is left alone (the
    emitter treats an association with no interpretable condition as unjoined
    and records that as a known loss rather than guessing).
    """
    if not isinstance(raw_on, list):
        return []
    conditions: list[JoinCondition] = []
    for index, token in enumerate(raw_on):
        if token != "=" or index == 0 or index + 1 >= len(raw_on):
            continue
        left, right = raw_on[index - 1], raw_on[index + 1]
        if not (isinstance(left, dict) and isinstance(right, dict)):
            continue
        left_ref, right_ref = left.get("ref"), right.get("ref")
        if not (isinstance(left_ref, list) and isinstance(right_ref, list)):
            continue
        # Normalize so the side rooted at the association is always `remote`.
        if left_ref and left_ref[0] == association_name:
            remote, local = left_ref, right_ref
        elif right_ref and right_ref[0] == association_name:
            remote, local = right_ref, left_ref
        else:
            continue
        conditions.append(
            JoinCondition(remote=tuple(str(p) for p in remote), local=tuple(str(p) for p in local))
        )
    return conditions


def _parse_element(name: str, raw: dict[str, Any]) -> Element:
    element = Element(
        name=name,
        type=raw.get("type"),
        length=raw.get("length"),
        precision=raw.get("precision"),
        scale=raw.get("scale"),
        is_key=bool(raw.get("key", False)),
        label=raw.get(LABEL_ANNOTATION),
        comment=raw.get("$comment"),
        enum=raw.get("enum") or {},
        target=raw.get("target"),
        cardinality=raw.get("cardinality") or {},
        annotations=_annotations(raw),
    )
    element.on = _parse_on(raw.get("on"), name)
    return element


def _parse_definition(name: str, raw: dict[str, Any]) -> Definition:
    definition = Definition(
        name=name,
        kind=raw.get("kind", "type"),
        label=raw.get(LABEL_ANNOTATION),
        comment=raw.get("$comment"),
        type=raw.get("type"),
        length=raw.get("length"),
        enum=raw.get("enum") or {},
        annotations=_annotations(raw),
    )
    for element_name, raw_element in (raw.get("elements") or {}).items():
        definition.elements[element_name] = _parse_element(element_name, raw_element)
    return definition


def parse_csn(document: dict[str, Any]) -> CsnModel:
    """Parse a CSN document (already loaded as JSON) into a :class:`CsnModel`."""
    model = CsnModel(
        csn_interop_effective=document.get("csnInteropEffective"),
        comment=document.get("$comment"),
    )
    for name, raw in (document.get("definitions") or {}).items():
        model.definitions[name] = _parse_definition(name, raw)
    return model


def load_csn(path: str | Path) -> CsnModel:
    """Load and parse a CSN document from disk."""
    with open(path, encoding="utf-8") as handle:
        return parse_csn(json.load(handle))
