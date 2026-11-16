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

"""Emission of ``ontology_mappings`` — ontology concepts to physical datasets.

CSN describes a model, not where that model's population lives, so the dataset
names, sources and column names come from the curation overlay. What this module
computes is the part that is genuinely derived: the *shape* of the mapping.
Walking the ontology's identity structure is what turns a flat column binding
into the nested ``referent_mappings`` that compound identity requires — a
sales-area row references its customer, which is itself referenced by business
partner number.
"""

from __future__ import annotations

import copy
from typing import Any

from .csn_to_ossie import ConceptPlan, Conversion, ConversionError, RelationshipPlan

#: Mapping expressions are emitted in the portable ANSI SQL core.
DEFAULT_DIALECT = "ANSI_SQL"

#: Core-spec version stamped on the semantic model embedded in each ontology map.
CORE_SPEC_VERSION = "0.2.0.dev0"


def _identity_chain(conversion: Conversion, concept: ConceptPlan) -> list[tuple[str, RelationshipPlan]]:
    """The identifying relationships of a concept, with their declaring concept.

    A role view declares no identity of its own — it inherits the parent's — so
    the chain follows ``extends`` until it finds one.
    """
    if concept.identify_by:
        chain: list[tuple[str, RelationshipPlan]] = []
        for name in concept.identify_by:
            found = _declaring(conversion, concept, name)
            if found is not None:
                chain.append(found)
        return chain
    for parent_name in concept.extends:
        parent = conversion.concept_by_name(parent_name)
        if parent is not None:
            chain = _identity_chain(conversion, parent)
            if chain:
                return chain
    return []


def _declaring(conversion: Conversion, concept: ConceptPlan, name: str) -> tuple[str, RelationshipPlan] | None:
    """Find the concept that declares relationship ``name``, searching ``extends``.

    A role view restates its parent's identifier by name, so the name resolves
    on the parent; the declaring concept is what qualifies the reference.
    """
    for relationship in concept.relationships:
        if relationship.name == name:
            return concept.name, relationship
    for parent_name in concept.extends:
        parent = conversion.concept_by_name(parent_name)
        if parent is not None:
            found = _declaring(conversion, parent, name)
            if found is not None:
                return found
    return None


def _qualified(declaring: str, owner: str, relationship: str) -> str:
    """Qualify a relationship reference only when it is not declared locally."""
    return relationship if declaring == owner else f"{declaring}.{relationship}"


def _expression(dataset: str, column: str) -> str:
    return f"{dataset}.{column}"


def _referent_mappings(
    conversion: Conversion,
    concept: ConceptPlan,
    dataset: str,
    binding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build referent mappings for a concept's identity from a column binding.

    ``binding`` maps CSN element names to dataset field names. When an
    identifying relationship points at another entity type, the binding value
    resolves against *that* entity's identity instead, producing a nested
    referent mapping.
    """
    mappings: list[dict[str, Any]] = []
    for declaring, relationship in _identity_chain(conversion, concept):
        if relationship.csn_element not in binding:
            raise ConversionError(
                f"concept {concept.name}: identity binding is missing CSN element "
                f"{relationship.csn_element!r} (identifying relationship {relationship.name!r})"
            )
        bound = binding[relationship.csn_element]
        entry: dict[str, Any] = {
            "relationship": _qualified(declaring, concept.name, relationship.name)
        }

        if relationship.role_is_entity:
            target = conversion.concept_by_name(relationship.role_concept)
            if target is None:
                raise ConversionError(
                    f"concept {concept.name}: identifying relationship {relationship.name!r} "
                    f"points at unknown concept {relationship.role_concept!r}"
                )
            target_chain = _identity_chain(conversion, target)
            if isinstance(bound, dict):
                nested_binding = dict(bound)
            elif len(target_chain) == 1:
                nested_binding = {target_chain[0][1].csn_element: bound}
            else:
                raise ConversionError(
                    f"concept {concept.name}: relationship {relationship.name!r} references "
                    f"{target.name}, which has a {len(target_chain)}-part identifier; bind each "
                    "part explicitly with a mapping instead of a single field name"
                )
            entry["referent_mappings"] = _referent_mappings(
                conversion, target, dataset, nested_binding
            )
        else:
            entry["expression"] = _expression(dataset, bound)
        mappings.append(entry)

    if not mappings:
        raise ConversionError(f"concept {concept.name}: no identifying relationships to map")
    return mappings


def _render_field(field: dict[str, Any]) -> dict[str, Any]:
    """Render a dataset field, expanding a bare column into a dialect expression."""
    name = field.get("name")
    if not name:
        raise ConversionError(f"dataset field is missing a name: {field!r}")
    expression = field.get("expression", name)
    rendered: dict[str, Any] = {"name": name}
    if isinstance(expression, dict):
        rendered["expression"] = expression
    else:
        rendered["expression"] = {
            "dialects": [{"dialect": DEFAULT_DIALECT, "expression": expression}]
        }
    for key in ("datatype", "description"):
        if field.get(key) is not None:
            rendered[key] = field[key]
    return rendered


def _render_semantic_model(semantic_model: dict[str, Any]) -> dict[str, Any]:
    # The ontology schema embeds a complete core-spec document here, so the
    # embedded model carries the core-spec version like any standalone one.
    rendered: dict[str, Any] = {"version": semantic_model.get("version", CORE_SPEC_VERSION)}
    for key in ("name", "description"):
        if semantic_model.get(key) is not None:
            rendered[key] = semantic_model[key]
    datasets: list[dict[str, Any]] = []
    for dataset in semantic_model.get("datasets") or []:
        entry: dict[str, Any] = {"name": dataset["name"]}
        for key in ("description", "source", "primary_key"):
            if dataset.get(key) is not None:
                entry[key] = dataset[key]
        entry["fields"] = [_render_field(f) for f in dataset.get("fields") or []]
        datasets.append(entry)
    rendered["datasets"] = datasets
    return rendered


def _render_concept_mapping(conversion: Conversion, binding: dict[str, Any]) -> dict[str, Any]:
    concept_name = binding.get("concept")
    concept = conversion.concept_by_name(concept_name)
    if concept is None:
        raise ConversionError(f"ontology mapping binds unknown concept {concept_name!r}")
    dataset = binding.get("dataset")
    if not dataset:
        raise ConversionError(f"concept {concept_name}: binding is missing `dataset`")

    identity_referents = _referent_mappings(
        conversion, concept, dataset, binding.get("identity") or {}
    )
    rendered: dict[str, Any] = {
        "concept": concept.name,
        "object_mappings": [{"referent_mappings": identity_referents}],
    }

    children: list[dict[str, Any]] = []
    for csn_element, column in (binding.get("attributes") or {}).items():
        relationship = concept.relationship_for_element(csn_element)
        if relationship is None:
            raise ConversionError(
                f"concept {concept.name}: attribute binding refers to CSN element "
                f"{csn_element!r}, which this concept does not expose as a relationship"
            )
        # A link mapping names its relationship bare: the spec's own example and
        # corpus do, and the mapping is already scoped to the concept.
        children.append(
            {
                "relationship": relationship.name,
                "object_mapping": {"expression": _expression(dataset, column)},
            }
        )
    if children:
        # A deep copy, not the same list object: sharing it would serialize as a
        # YAML anchor and alias, which is valid but not what a fixture should be
        # diffed against.
        rendered["link_mappings"] = [
            {
                "object_mapping": {"referent_mappings": copy.deepcopy(identity_referents)},
                "children": children,
            }
        ]
    return rendered


def render_ontology_mappings(conversion: Conversion) -> list[dict[str, Any]]:
    """Render every ontology map declared in the curation overlay."""
    rendered: list[dict[str, Any]] = []
    for entry in conversion.curation.ontology_mappings:
        semantic_model = entry.get("semantic_model")
        if not semantic_model:
            raise ConversionError("ontology mapping is missing `semantic_model`")
        ontology_map: dict[str, Any] = {}
        for key in ("name", "description"):
            if entry.get(key) is not None:
                ontology_map[key] = entry[key]
        ontology_map["semantic_model"] = _render_semantic_model(semantic_model)
        ontology_map["concept_mappings"] = [
            _render_concept_mapping(conversion, binding)
            for binding in entry.get("concept_bindings") or []
        ]
        rendered.append(ontology_map)
    return rendered
