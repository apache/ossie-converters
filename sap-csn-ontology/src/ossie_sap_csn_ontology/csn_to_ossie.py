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

"""CSN to Apache Ossie ontology emitter.

The mapping rules implemented here follow the CSN/Ossie semantics document
(``docs/csn-mapping.md``), construct by construct:

===========================================  ==========================================
CSN construct                                Apache Ossie construct
===========================================  ==========================================
``kind: type`` with ``enum``                 ``ValueType`` + closed-world ``requires``
Scalar element with a domain                 ``ValueType`` ``extends`` a built-in
``length``                                   recorded in ``description``, reported dropped
``key`` element                              Identifying relationship + ``identify_by``
Compound ``key``                             Several identifying relationships
``cds.Boolean`` element                      Unary relationship (no ``roles``)
Association to one, joined on target key     Relationship to the target ``EntityType``
Association to one, managed (no ``on``)      Relationship to the target ``EntityType``
Association to one, joined on source key     Relationship to the target's identifier
Association to many                          Relationship with no ``multiplicity``
Association to many, inverse of a FK         Reverse verbalization on that FK relationship
Role view of another entity                  ``extends`` + ``derived_by EXISTS(...)``
``@EndUserText.label``                       ``description`` and ``verbalizes``
===========================================  ==========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .csn import CsnModel, Definition, Element
from .curation import Curation
from .naming import relationship_name, value_type_name

#: The ontology specification version this emitter targets.
ONTOLOGY_VERSION = "0.2.0.dev0"

#: Emission role of a CSN element, decided by :func:`_plan_entity`.
IDENTITY_SCALAR = "identity_scalar"
ATTRIBUTE_SCALAR = "attribute_scalar"
FLAG = "flag"
FOREIGN_KEY_ASSOCIATION = "foreign_key_association"
BACK_REFERENCE_ASSOCIATION = "back_reference_association"
COLLECTION_ASSOCIATION = "collection_association"


class ConversionError(ValueError):
    """Raised when a CSN document cannot be expressed in the ontology spec."""


@dataclass
class ValueTypeSpec:
    """A value-type concept, before curation renames are applied."""

    derived_name: str
    extends: str
    description: str | None = None
    requires: list[str] = field(default_factory=list)


@dataclass
class RelationshipPlan:
    """One emitted relationship, with the CSN element it came from."""

    kind: str
    csn_element: str
    name: str
    role_concept: str | None = None
    role_is_entity: bool = False
    multiplicity: str | None = None
    identifying: bool = False
    description: str | None = None
    verbalizes: list[str] = field(default_factory=list)


@dataclass
class ConceptPlan:
    """One emitted entity-type concept."""

    name: str
    definition: Definition
    description: str | None = None
    extends: list[str] = field(default_factory=list)
    derived_by: list[str] = field(default_factory=list)
    identify_by: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    relationships: list[RelationshipPlan] = field(default_factory=list)

    def relationship_for_element(self, csn_element: str) -> RelationshipPlan | None:
        for relationship in self.relationships:
            if relationship.csn_element == csn_element:
                return relationship
        return None


@dataclass
class RoleView:
    """A CSN entity that is a role view of another (SAP business-partner roles)."""

    parent: Definition
    association: Element
    #: Element on the role view that carries the parent's key.
    back_reference_element: str


@dataclass
class Conversion:
    """The full emission plan, kept as data so tests can assert on rules."""

    model: CsnModel
    curation: Curation
    value_types: dict[str, ValueTypeSpec] = field(default_factory=dict)
    concepts: list[ConceptPlan] = field(default_factory=list)
    role_views: dict[str, RoleView] = field(default_factory=dict)
    #: target entity -> [(source entity, to-many association, target FK element)]
    inverse_associations: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: ``definition[.element]: @annotation`` for every annotation not carried.
    dropped_annotations: list[str] = field(default_factory=list)

    def concept_by_name(self, name: str) -> ConceptPlan | None:
        for concept in self.concepts:
            if concept.name == name:
                return concept
        return None


# ---------------------------------------------------------------------------
# Constraint expressions
# ---------------------------------------------------------------------------


def _literal(value: Any) -> str:
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else f"'{value}'"


def fixed_value_constraint(concept: str, enum: dict[str, Any]) -> str:
    """CSN fixed values become a closed-world constraint.

    The ontology expression language has comparison and boolean operators but
    no ``IN``, so the closed set is written as a disjunction of equalities, the
    form the spec's own corpus uses. Declaration order is preserved so the
    emitted expression is stable.
    """
    return " OR ".join(f"{concept} == {_literal(entry.get('val'))}" for entry in enum.values())


def length_note(length: int) -> str:
    """A CSN ``length`` is recorded, not modeled.

    The ontology expression language defines no functions, so there is no
    portable way to state ``LENGTH(x) <= n`` as a ``requires`` constraint. The
    length is carried into the value type's description and reported as a
    dropped constraint, never silently lost.
    """
    return f"CSN length {length}."


def mandatory_constraint(concept: str, relationship: str) -> str:
    """Each part of a compound identifier is required to exist."""
    return f"{concept}.{relationship}"


def role_derivation(parent_concept: str, relationship: str) -> str:
    """The closed-world reading of an SAP role: the parent carries the role key."""
    return f"EXISTS ( {parent_concept}.{relationship} )"


# ---------------------------------------------------------------------------
# Descriptions and verbalizations
# ---------------------------------------------------------------------------


def _joined(*parts: str | None) -> str | None:
    kept = [p.strip() for p in parts if p and p.strip()]
    return " ".join(kept) or None


def _default_concept_description(definition: Definition) -> str | None:
    label = f"{definition.label}." if definition.label else None
    return _joined(label, f"CSN source: entity {definition.simple_name}.", definition.comment)


def _default_element_description(element: Element) -> str | None:
    label = f"{element.label}." if element.label else None
    return _joined(label, f"CSN element {element.name}.", element.comment)


def _default_verbalizations(plan: RelationshipPlan, concept_name: str, element: Element) -> list[str]:
    """Template verbalizations, used when the overlay supplies none.

    ``verbalizes`` is required by the ontology schema, so the emitter always
    produces at least one sentence. These templates are structural placeholders:
    a curated model replaces them with the language its agents should read.
    """
    subject = f"{{{concept_name}}}"
    if plan.kind == FLAG:
        return [f"{subject} is flagged {element.label or element.name}"]
    role = f"{{{plan.role_concept}}}"
    if plan.identifying and plan.multiplicity == "OneToOne":
        return [f"{subject} is identified by {role}", f"{role} identifies {subject}"]
    if plan.kind == COLLECTION_ASSOCIATION:
        return [f"{subject} has many {role}", f"{role} belongs to {subject}"]
    if plan.role_is_entity:
        return [f"{subject} references {role}", f"{role} is referenced by {subject}"]
    return [f"{subject} has {role}"]


# ---------------------------------------------------------------------------
# Structural analysis
# ---------------------------------------------------------------------------


def _target(model: CsnModel, element: Element) -> Definition | None:
    return model.resolve(element.target)


def _detect_role_views(model: CsnModel) -> dict[str, RoleView]:
    """Find entities that are role views of another entity.

    An SAP role view (``I_Customer`` over ``BusinessPartner``) shows up in CSN as
    a to-one association from the parent to the role entity whose ``ON``
    condition equates the *parent's own key* with a *non-key* element of the role
    entity. That direction is the tell: a plain foreign key points at the
    target's key, a role view points back at the source's.
    """
    role_views: dict[str, RoleView] = {}
    for parent in model.entities():
        parent_keys = {e.name for e in parent.key_elements}
        for element in parent.elements.values():
            if not element.is_association or element.is_to_many:
                continue
            target = _target(model, element)
            if target is None or target.kind != "entity":
                continue
            target_keys = {e.name for e in target.key_elements}
            for condition in element.on:
                remote, local = condition.remote_element, condition.local_element
                if local in parent_keys and remote in target.elements and remote not in target_keys:
                    role_views.setdefault(
                        target.name,
                        RoleView(parent=parent, association=element, back_reference_element=remote),
                    )
    return role_views


def _inverse_fold(model: CsnModel, source: Definition, association: Element, target: Definition) -> str | None:
    """The target's foreign-key element that a to-many association is the inverse of.

    ``Customer._SalesArea`` (to many, ``ON _SalesArea.Customer = Customer``) and
    ``CustomerSalesArea._Customer`` (to one, ``ON _Customer.Customer = Customer``)
    describe one link. Returns the target's local element (``Customer``) when the
    target carries a to-one association back to ``source`` joined on the same
    element pair; ``None`` otherwise.
    """
    if not association.on:
        return None
    source_keys = {e.name for e in source.key_elements}
    for condition in association.on:
        remote, local = condition.remote_element, condition.local_element
        if local not in source_keys or remote not in target.elements:
            continue
        for back in target.elements.values():
            if not back.is_association or back.is_to_many or back.target != source.name:
                continue
            for back_condition in back.on:
                if back_condition.local_element == remote and back_condition.remote_element == local:
                    return remote
    return None


def _identifying_value_type(conversion: Conversion, definition: Definition) -> str | None:
    """The value type a single-key entity is referenced by, if it has one."""
    keys = definition.key_elements
    if len(keys) != 1 or keys[0].is_association:
        return None
    derived = value_type_name(keys[0].label, keys[0].name)
    return conversion.curation.value_type_name(derived) if derived in conversion.value_types else None


def _plan_entity(conversion: Conversion, definition: Definition) -> ConceptPlan:
    """Decide what each element of an entity becomes."""
    model, curation = conversion.model, conversion.curation
    simple_name = definition.simple_name
    role_view = conversion.role_views.get(definition.name)

    concept = ConceptPlan(
        name=simple_name,
        definition=definition,
        description=curation.concept_description(
            simple_name, _default_concept_description(definition)
        ),
    )

    # Elements consumed by another construct rather than emitted on their own:
    # a folded foreign key, and the two elements a role view inherits.
    consumed: dict[str, Element] = {}
    folds: dict[str, Element] = {}  # local FK element name -> association element
    for element in definition.elements.values():
        if not element.is_association or element.is_to_many:
            continue
        target = _target(model, element)
        if target is None:
            conversion.warnings.append(
                f"{definition.name}.{element.name}: association target {element.target!r} is not "
                "in this CSN document; relationship omitted"
            )
            continue
        target_keys = {e.name for e in target.key_elements}
        for condition in element.on:
            if condition.remote_element in target_keys and condition.local_element in definition.elements:
                folds[condition.local_element] = element
                consumed[element.name] = element
                break

    if role_view is not None:
        # A role view inherits identity from its parent, so neither its own key
        # nor its foreign key back to the parent becomes a relationship.
        for key_element in definition.key_elements:
            consumed[key_element.name] = key_element
        back_reference = definition.elements.get(role_view.back_reference_element)
        if back_reference is not None:
            consumed[back_reference.name] = back_reference
        parent_concept = role_view.parent.simple_name
        parent_relationship = relationship_name(role_view.association.name, parent_concept)
        parent_relationship = curation.relationship(
            parent_concept, role_view.association.name
        ).get("rename", parent_relationship)
        concept.extends = [parent_concept]
        concept.derived_by = [role_derivation(parent_concept, parent_relationship)]
        # The spec does not say whether `identify_by` is inherited through
        # `extends`, so a role view restates its parent's identifier explicitly.
        parent_plan = conversion.concept_by_name(parent_concept)
        if parent_plan is not None:
            concept.identify_by = list(parent_plan.identify_by)

    for element in definition.elements.values():
        if element.name in consumed:
            continue

        if element.name in folds:
            association = folds[element.name]
            target = _target(model, association)
            plan = RelationshipPlan(
                kind=FOREIGN_KEY_ASSOCIATION,
                csn_element=element.name,
                name=relationship_name(element.name, simple_name),
                role_concept=target.simple_name,
                role_is_entity=True,
                multiplicity="ManyToOne",
                identifying=element.is_key,
            )
            source_element = element
        elif element.is_association:
            target = _target(model, element)
            if target is None:
                continue
            if element.is_to_many and _inverse_fold(model, definition, element, target) is not None:
                # The target already holds this fact as a foreign-key relationship
                # back to us. One relationship, verbalized in both directions,
                # rather than two declarations of the same link.
                conversion.inverse_associations.setdefault(target.name, []).append(
                    (definition.name, element.name, _inverse_fold(model, definition, element, target))
                )
                continue
            if element.is_to_many:
                plan = RelationshipPlan(
                    kind=COLLECTION_ASSOCIATION,
                    csn_element=element.name,
                    name=relationship_name(element.name, simple_name, to_many=True),
                    role_concept=target.simple_name,
                    role_is_entity=True,
                )
            elif not element.on:
                # A managed association declares no ON condition: CAP generates
                # the foreign key from the target's own key, so this is a plain
                # reference to the target entity.
                plan = RelationshipPlan(
                    kind=FOREIGN_KEY_ASSOCIATION,
                    csn_element=element.name,
                    name=relationship_name(element.name, simple_name),
                    role_concept=target.simple_name,
                    role_is_entity=True,
                    multiplicity="ManyToOne",
                    identifying=element.is_key,
                )
            else:
                # A to-one association joined on this entity's own key: it
                # carries the target's identifier, not the target's population.
                role_concept = _identifying_value_type(conversion, target)
                if role_concept is None:
                    role_concept, role_is_entity = target.simple_name, True
                    conversion.warnings.append(
                        f"{definition.name}.{element.name}: target {target.simple_name} has no "
                        "single-element identifier; relationship points at the entity instead of "
                        "its identifier"
                    )
                else:
                    role_is_entity = False
                plan = RelationshipPlan(
                    kind=BACK_REFERENCE_ASSOCIATION,
                    csn_element=element.name,
                    name=relationship_name(element.name, simple_name),
                    role_concept=role_concept,
                    role_is_entity=role_is_entity,
                    multiplicity="ManyToOne",
                )
            source_element = element
        elif element.builtin == "Boolean":
            # A flag is a proposition about the entity, not a value it has, so
            # it becomes a unary relationship with no roles.
            plan = RelationshipPlan(
                kind=FLAG,
                csn_element=element.name,
                name=relationship_name(element.name, simple_name),
            )
            source_element = element
        else:
            derived = value_type_name(element.label, element.name)
            plan = RelationshipPlan(
                kind=IDENTITY_SCALAR if element.is_key else ATTRIBUTE_SCALAR,
                csn_element=element.name,
                name=relationship_name(element.name, simple_name),
                role_concept=curation.value_type_name(derived),
                multiplicity="ManyToOne",
                identifying=element.is_key,
            )
            source_element = element

        overlay = curation.relationship(simple_name, plan.csn_element)
        plan.name = overlay.get("rename", plan.name)
        plan.description = curation.relationship_description(
            simple_name, plan.csn_element, _default_element_description(source_element)
        )
        concept.relationships.append(plan)

    identifying = [r for r in concept.relationships if r.identifying]
    if len(identifying) == 1:
        # A single-element identifier is one-to-one in both directions.
        identifying[0].multiplicity = "OneToOne"
    if identifying or not concept.identify_by:
        concept.identify_by = [r.name for r in identifying]
    if len(identifying) > 1:
        # Compound identity: every part must exist for the entity to be identified.
        concept.requires = [mandatory_constraint(concept.name, r.name) for r in identifying]

    for plan in concept.relationships:
        overlay = curation.relationship(simple_name, plan.csn_element)
        plan.verbalizes = list(overlay.get("verbalizes") or []) or _default_verbalizations(
            plan, concept.name, definition.elements[plan.csn_element]
        )

    # Reverse readings contributed by inverse to-many associations on other entities.
    for source_name, source_element, local_fk in conversion.inverse_associations.get(definition.name, []):
        plan = concept.relationship_for_element(local_fk)
        source = model.definitions[source_name]
        if plan is None:
            continue
        overlay = curation.relationship(source.simple_name, source_element)
        reverse = list(overlay.get("verbalizes") or []) or [
            f"{{{source.simple_name}}} has many {{{concept.name}}}",
        ]
        plan.verbalizes.extend(v for v in reverse if v not in plan.verbalizes)

    return concept


def _collect_value_types(conversion: Conversion) -> None:
    """Build the value-type registry from CSN named types and element domains."""
    model, curation = conversion.model, conversion.curation
    # Constraints as derived from CSN, before curation overrides, so that the
    # duplicate check below compares like with like.
    derived_requires: dict[str, list[str]] = {}

    def register(derived: str, extends: str, description: str | None, requires: list[str]) -> None:
        existing = conversion.value_types.get(derived)
        if existing is not None:
            if existing.extends != extends or derived_requires[derived] != requires:
                conversion.warnings.append(
                    f"value type {curation.value_type_name(derived)}: CSN elements sharing this "
                    "domain name disagree on type or constraints; keeping the first definition"
                )
            return
        derived_requires[derived] = list(requires)
        conversion.value_types[derived] = ValueTypeSpec(
            derived_name=derived,
            extends=extends,
            description=curation.value_type_description(derived, description),
            requires=curation.value_type_requires(derived, requires),
        )

    for definition in model.definitions.values():
        if definition.kind != "type" or not definition.builtin:
            continue
        derived = value_type_name(definition.label, definition.simple_name)
        final = curation.value_type_name(derived)
        requires: list[str] = []
        description = _joined(definition.label, definition.comment)
        if definition.enum:
            requires.append(fixed_value_constraint(final, definition.enum))
        elif definition.length is not None and definition.builtin == "String":
            description = _joined(description, length_note(definition.length))
            conversion.warnings.append(
                f"value type {final}: CSN length {definition.length} recorded in the description; "
                "the ontology expression language cannot express a length constraint"
            )
        register(derived, definition.builtin, description, requires)

    for definition in model.entities():
        for element in definition.elements.values():
            if element.is_association or element.builtin in (None, "Boolean"):
                continue
            named_type = model.resolve(element.type)
            if named_type is not None and named_type.kind == "type":
                continue  # already registered from the named type itself
            derived = value_type_name(element.label, element.name)
            final = curation.value_type_name(derived)
            requires = []
            description = _joined(element.label, element.comment)
            if element.enum:
                requires.append(fixed_value_constraint(final, element.enum))
            elif element.length is not None and element.builtin == "String":
                description = _joined(description, length_note(element.length))
                if derived not in conversion.value_types:
                    conversion.warnings.append(
                        f"value type {final}: CSN length {element.length} recorded in the description; "
                        "the ontology expression language cannot express a length constraint"
                    )
            register(derived, element.builtin, description, requires)


#: The one CSN annotation the ontology layer has a home for (as ``description``).
CARRIED_ANNOTATIONS = frozenset({"@EndUserText.label"})


def _report_dropped_annotations(conversion: Conversion) -> None:
    """Every annotation other than the label is dropped, and said so.

    ``OntologyComponent``, ``Relationship`` and ``Role`` accept no
    ``custom_extensions`` and no per-concept ``ai_context``, so there is nowhere
    to carry ``@ODM.entityName``, ``@Semantics.*`` or ``@ObjectModel.*``. Until
    the ontology schema gains an annotations construct they are reported as
    dropped rather than lost silently.
    """
    for definition in conversion.model.definitions.values():
        for key in sorted(set(definition.annotations) - CARRIED_ANNOTATIONS):
            conversion.dropped_annotations.append(f"{definition.name}: {key}")
        for element in definition.elements.values():
            for key in sorted(set(element.annotations) - CARRIED_ANNOTATIONS):
                conversion.dropped_annotations.append(f"{definition.name}.{element.name}: {key}")
    if conversion.dropped_annotations:
        conversion.warnings.append(
            f"{len(conversion.dropped_annotations)} CSN annotation(s) have no home in the ontology "
            "layer and were dropped: " + "; ".join(conversion.dropped_annotations)
        )


def convert(model: CsnModel, curation: Curation | None = None) -> Conversion:
    """Analyze a CSN model and produce the emission plan."""
    conversion = Conversion(model=model, curation=curation or Curation())
    conversion.role_views = _detect_role_views(model)
    _collect_value_types(conversion)
    for definition in model.entities():
        conversion.concepts.append(_plan_entity(conversion, definition))
    _report_dropped_annotations(conversion)
    return conversion


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_value_type(conversion: Conversion, spec: ValueTypeSpec) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "concept": conversion.curation.value_type_name(spec.derived_name),
        "type": "ValueType",
    }
    if spec.description:
        rendered["description"] = spec.description
    rendered["extends"] = [spec.extends]
    if spec.requires:
        rendered["requires"] = list(spec.requires)
    return rendered


def _render_relationship(plan: RelationshipPlan) -> dict[str, Any]:
    rendered: dict[str, Any] = {"name": plan.name}
    if plan.description:
        rendered["description"] = plan.description
    if plan.role_concept is not None:
        rendered["roles"] = [{"concept": plan.role_concept}]
    if plan.multiplicity is not None:
        rendered["multiplicity"] = plan.multiplicity
    rendered["verbalizes"] = list(plan.verbalizes)
    return rendered


def _render_concept(plan: ConceptPlan) -> dict[str, Any]:
    rendered: dict[str, Any] = {"concept": plan.name, "type": "EntityType"}
    if plan.description:
        rendered["description"] = plan.description
    if plan.extends:
        rendered["extends"] = list(plan.extends)
    if plan.derived_by:
        rendered["derived_by"] = list(plan.derived_by)
    if plan.identify_by:
        rendered["identify_by"] = list(plan.identify_by)
    if plan.requires:
        rendered["requires"] = list(plan.requires)
    if plan.relationships:
        rendered["relationships"] = [_render_relationship(r) for r in plan.relationships]
    return rendered


def render(conversion: Conversion, *, name: str | None = None, description: str | None = None) -> dict[str, Any]:
    """Render an emission plan as an ontology document.

    Value types are emitted before entity types, each in stable derivation
    order, so the output is byte-identical for identical inputs.
    """
    from .mappings import render_ontology_mappings  # local import: mappings depend on this module

    curation_model = conversion.curation.model
    model_name = name or curation_model.get("name")
    if not model_name:
        raise ConversionError(
            "no ontology name: pass name= or set `model.name` in the curation overlay"
        )

    document: dict[str, Any] = {"version": ONTOLOGY_VERSION, "name": model_name}
    model_description = description or curation_model.get("description")
    if model_description:
        document["description"] = model_description

    document["ontology"] = [
        _render_value_type(conversion, spec) for spec in conversion.value_types.values()
    ] + [_render_concept(plan) for plan in conversion.concepts]

    mappings = render_ontology_mappings(conversion)
    if mappings:
        document["ontology_mappings"] = mappings
    return document


def csn_to_ossie(
    csn_document: dict[str, Any],
    curation_document: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Convert a CSN document to an Apache Ossie ontology document."""
    from .csn import parse_csn
    from .curation import parse_curation

    conversion = convert(parse_csn(csn_document), parse_curation(curation_document))
    return render(conversion, name=name, description=description)
