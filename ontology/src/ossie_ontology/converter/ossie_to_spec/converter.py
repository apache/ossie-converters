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

"""Reverse converter: OssieOntology (runtime) -> `ossie.models.OssieOntologyModel` (Pydantic DTO).

Pairs with spec_to_ossie.SpecToOssieConverter so a full round-trip
yaml -> spec -> model -> spec -> yaml is structurally stable."""

from __future__ import annotations

from typing import Any

from ossie.models import (
    OssieConcept,
    OssieConceptMapping,
    OssieConceptType,
    OssieCustomExtension,
    OssieDataType,
    OssieDataset,
    OssieDialectExpression,
    OssieDimension,
    OssieExpression,
    OssieField,
    OssieLinkMapping,
    OssieMetric,
    OssieMultiplicity,
    OssieObjectMapping,
    OssieOntologyMapping,
    OssieOntologyModel,
    OssieOntologyRelationship,
    OssieReferentMapping,
    OssieRelationship,
    OssieRole,
    OssieSemanticModel,
)

from ossie_ontology.model import (
    Concept,
    ConceptMapping,
    ConceptType,
    CustomExtension,
    Dataset,
    DatasetField,
    DialectExpressionSet,
    Dimension,
    JoinPath,
    LinkMapping,
    SemanticModel,
    Metric,
    ObjectMapping,
    OntologyComponent,
    OntologyMapping,
    ReferentMapping,
    Relationship,
    OssieOntology,
)

# The shared models leave an absent optional list as None, and serialization
# omits only None, so an empty runtime list is written `... or None` to keep it
# out of the YAML (and, where the schema says `minItems: 1`, valid).


class OssieToSpecConverter:
    """Top-level reverse converter."""

    @staticmethod
    def convert(model: OssieOntology) -> OssieOntologyModel:
        ont = model.ontology
        ontology_mappings = [_convert_ontology_mapping(ontology_mapping) for ontology_mapping in model.ontology_mappings]
        # Without a version the document takes the one the schema pins.
        version: dict[str, Any] = {"version": model.version} if model.version is not None else {}
        return OssieOntologyModel(
            **version,
            name=model.name,
            description=model.description,
            ai_context=model.ai_context,
            requires=[f.raw_expr for f in ont.requires] or None,
            ontology=_convert_ontology_concepts(ont),
            ontology_mappings=ontology_mappings or None,
            prefixes=model.prefixes or None,
        )


# ---------------------------------------------------------------------------
# Ontology
# ---------------------------------------------------------------------------

def _convert_ontology_concepts(ont: OntologyComponent) -> list[OssieConcept]:
    components: list[OssieConcept] = []
    # Group once: `ont.relationships` rebuilds a list on every access, so scanning
    # it per concept is O(concepts x relationships). Keyed on id() to keep the
    # identity comparison a per-concept scan would do.
    by_container: dict[int, list[Relationship]] = {}
    for rel in ont.relationships:
        by_container.setdefault(id(rel.container), []).append(rel)

    for concept in ont.concepts():
        if not concept.is_component:
            continue
        components.append(_convert_concept(concept, by_container.get(id(concept), [])))
    return components


def _convert_concept(concept: Concept, rels: list[Relationship]) -> OssieConcept:
    if not isinstance(concept.type, ConceptType):
        raise ValueError(f"Concept '{concept.name}' has no type; the schema requires one.")
    extends = [p.name for p in concept.extends] if concept.extends else None

    return OssieConcept(
        concept=concept.name,
        type=OssieConceptType(concept.type.value),
        description=concept.description,
        extends=extends,
        identify_by=[rel.name for rel in concept.identify_by.values()] or None,
        derived_by=[f.raw_expr for f in concept.derived_by] or None,
        requires=[f.raw_expr for f in concept.requires] or None,
        relationships=[_convert_relationship(rel) for rel in rels] or None,
        iri=concept.iri,
    )


def _convert_relationship(rel: Relationship) -> OssieOntologyRelationship:
    extra_roles = list(rel.roles)[1:]
    roles = [OssieRole(concept=role.player.name, name=role.explicit_name) for role in extra_roles]

    multiplicity = OssieMultiplicity(rel.multiplicity.value) if rel.multiplicity is not None else None
    # Required by the schema, so written out even when empty.
    verbalizes = rel.verbalizes_raw if rel.verbalizes_raw is not None else []

    return OssieOntologyRelationship(
        name=rel.name,
        description=rel.description,
        roles=roles or None,
        verbalizes=verbalizes,
        multiplicity=multiplicity,
        derived_by=[f.raw_expr for f in rel.derived_by] or None,
        requires=[f.raw_expr for f in rel.requires] or None,
        iri=rel.iri,
    )


# ---------------------------------------------------------------------------
# Semantic model
# ---------------------------------------------------------------------------

def _convert_semantic_model(semantic_model: SemanticModel) -> OssieSemanticModel:
    return OssieSemanticModel(
        version=semantic_model.version,
        name=semantic_model.name,
        description=semantic_model.description,
        ai_context=semantic_model.ai_context,
        datasets=[_convert_dataset(ds) for ds in semantic_model.datasets],
        relationships=[_convert_join_path(jp) for jp in semantic_model.join_paths] or None,
        metrics=[_convert_metric(metric) for metric in semantic_model.metrics] or None,
        custom_extensions=[_convert_custom_extension(ce) for ce in semantic_model.custom_extensions] or None,
    )


def _convert_dataset(ds: Dataset) -> OssieDataset:
    return OssieDataset(
        name=ds.name,
        source=ds.source,
        primary_key=ds.primary_key,
        unique_keys=ds.unique_keys,
        description=ds.description,
        ai_context=ds.ai_context,
        fields=[_convert_dataset_field(fl) for fl in ds.fields] or None,
        custom_extensions=[_convert_custom_extension(ce) for ce in ds.custom_extensions] or None,
    )


def _convert_dataset_field(fl: DatasetField) -> OssieField:
    return OssieField(
        name=fl.name,
        expression=_convert_expression(fl.expression),
        datatype=OssieDataType(fl.datatype.value) if fl.datatype is not None else None,
        dimension=_convert_dimension(fl.dimension),
        label=fl.label,
        description=fl.description,
        ai_context=fl.ai_context,
        custom_extensions=[_convert_custom_extension(ce) for ce in fl.custom_extensions] or None,
    )


def _convert_expression(es: DialectExpressionSet) -> OssieExpression:
    return OssieExpression(
        sql=es.sql,
        dialects=[OssieDialectExpression(dialect=d.dialect, expression=d.expression) for d in es.dialects] or None,
    )


def _convert_dimension(dim: Dimension | None) -> OssieDimension | None:
    if dim is None:
        return None
    return OssieDimension(is_time=dim.is_time)


def _convert_join_path(jp: JoinPath) -> OssieRelationship:
    return OssieRelationship(
        name=jp.name,
        from_dataset=jp.from_dataset.name,
        to=jp.to_dataset.name,
        from_columns=[from_col.name for from_col in jp.from_columns],
        to_columns=[to_col.name for to_col in jp.to_columns],
        ai_context=jp.ai_context,
        custom_extensions=[_convert_custom_extension(ce) for ce in jp.custom_extensions] or None,
    )


def _convert_metric(metric: Metric) -> OssieMetric:
    return OssieMetric(
        name=metric.name,
        expression=_convert_expression(metric.expression),
        description=metric.description,
        ai_context=metric.ai_context,
        datatype=OssieDataType(metric.datatype.value) if metric.datatype is not None else None,
        custom_extensions=[_convert_custom_extension(ce) for ce in metric.custom_extensions] or None,
    )


def _convert_custom_extension(ce: CustomExtension) -> OssieCustomExtension:
    return OssieCustomExtension(vendor_name=ce.vendor_name, data=ce.data)


# ---------------------------------------------------------------------------
# Ontology mapping (tree)
# ---------------------------------------------------------------------------

def _convert_ontology_mapping(ontology_mapping: OntologyMapping) -> OssieOntologyMapping:
    return OssieOntologyMapping(
        name=ontology_mapping.name,
        description=ontology_mapping.description,
        semantic_model=_convert_semantic_model(ontology_mapping.semantic_model),
        concept_mappings=[_convert_concept_mapping(concept_mapping) for concept_mapping in ontology_mapping.concept_mappings],
    )


def _convert_concept_mapping(concept_mapping: ConceptMapping) -> OssieConceptMapping:
    return OssieConceptMapping(
        concept=concept_mapping.concept.name,
        object_mappings=[_convert_object_mapping(om) for om in concept_mapping.object_mappings] or None,
        link_mappings=[_convert_link_mapping(lm) for lm in concept_mapping.link_mappings] or None,
    )


def _convert_object_mapping(object_mapping: ObjectMapping) -> OssieObjectMapping:
    referent_mappings = None
    if object_mapping.referent_mappings is not None:
        referent_mappings = [_convert_referent_mapping(rm) for rm in object_mapping.referent_mappings]
    return OssieObjectMapping(
        concept=object_mapping.concept.name if object_mapping.concept is not None else None,
        expression=_render_mapping_expression(object_mapping.expression),
        referent_mappings=referent_mappings,
    )


def _convert_referent_mapping(referent_mapping: ReferentMapping) -> OssieReferentMapping:
    nested = None
    if referent_mapping.referent_mappings is not None:
        nested = [_convert_referent_mapping(child) for child in referent_mapping.referent_mappings]
    return OssieReferentMapping(
        relationship=referent_mapping.relationship.name,
        expression=_render_mapping_expression(referent_mapping.expression),
        referent_mappings=nested,
    )


def _render_mapping_expression(expr) -> str | None:
    """Reconstruct the source string for a parsed mapping expression. The
    runtime model carries either a `DatasetField` (single field reference)
    or a `Formula` (richer expression); both round-trip back to the same
    string the forward converter saw in the spec."""
    if expr is None:
        return None
    from ossie_ontology.model import DatasetField as _DF, Formula as _F
    if isinstance(expr, _DF):
        ds = expr.dataset
        return f"{ds.name}.{expr.name}" if ds is not None else expr.name
    if isinstance(expr, _F):
        return expr.raw_expr
    return str(expr)


def _convert_link_mapping(link_mapping: LinkMapping) -> OssieLinkMapping:
    children = None
    if link_mapping.children is not None:
        children = [_convert_link_mapping(child) for child in link_mapping.children]
    return OssieLinkMapping(
        object_mapping=_convert_object_mapping(link_mapping.object_mapping),
        relationship=link_mapping.relationship.name if link_mapping.relationship is not None else None,
        children=children,
    )
