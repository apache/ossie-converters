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

from dataclasses import dataclass, replace
from typing import Callable, Mapping, Optional, Sequence

import jinja2
from jinja2.sandbox import SandboxedEnvironment
from sqlglot import exp

from metricflow_semantic_interfaces.protocols.semantic_model import SemanticModel
from metricflow_semantic_interfaces.protocols.where_filter import WhereFilterIntersection
from metricflow_semantic_interfaces.type_enums import DimensionType, EntityType
from metricflow_semantics.errors.error_classes import FeatureNotSupportedError
from metricflow_semantics.naming.linkable_spec_name import StructuredLinkableSpecName
from ossie import OssieDataset, OssieRelationship
from ossie_dbt.expression_utils import column_span, parse_scalar_sql, qualified_column, replace_columns_in_sql


class UnresolvedFilterError(Exception):
    """A dbt filter could not be represented as an Ossie predicate."""


@dataclass(frozen=True)
class _FilterReference:
    _kind: str
    _name: str
    _path: Sequence[str]
    _resolve: Callable[[str, str, Sequence[str], Optional[str]], str]
    _time_modifier: Optional[str] = None

    def grain(self, time_granularity: str) -> "_FilterReference":
        return replace(self, _time_modifier=time_granularity)

    def date_part(self, date_part_name: str) -> "_FilterReference":
        return self.grain(date_part_name)

    def descending(self, is_descending: bool) -> "_FilterReference":
        if is_descending:
            raise UnresolvedFilterError("Sort order cannot be used in a filter")
        return self

    def __str__(self) -> str:
        return self._resolve(self._kind, self._name, self._path, self._time_modifier)


def _render_filter_template(
    template: str,
    resolve: Callable[[str, str, Sequence[str], Optional[str]], str],
) -> str:
    """Render a dbt predicate using a sandbox and semantic references supplied by the caller."""
    def dimension(name: str, entity_path: Sequence[str] = ()) -> _FilterReference:
        return _FilterReference("Dimension", name, entity_path, resolve)

    def time_dimension(
        time_dimension_name: str,
        time_granularity_name: Optional[str] = None,
        entity_path: Sequence[str] = (),
        descending: Optional[bool] = None,
        date_part_name: Optional[str] = None,
    ) -> _FilterReference:
        reference = _FilterReference(
            "TimeDimension", time_dimension_name, entity_path, resolve, time_granularity_name or date_part_name
        )
        return reference.descending(descending) if descending else reference

    def entity(entity_name: str, entity_path: Sequence[str] = ()) -> _FilterReference:
        return _FilterReference("Entity", entity_name, entity_path, resolve)

    def metric(name: str, group_by: Sequence[str] = ()) -> _FilterReference:
        return _FilterReference("Metric", name, (), resolve)

    return (
        SandboxedEnvironment(undefined=jinja2.StrictUndefined)
        .from_string(template)
        .render(Dimension=dimension, TimeDimension=time_dimension, Entity=entity, Metric=metric)
    )


def collect_filter_templates(where: Optional[WhereFilterIntersection]) -> tuple[str, ...]:
    if where is None:
        return ()
    return tuple(template for entry in where.where_filters if (template := entry.where_sql_template.strip()))


def merge_filter_templates(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(template for group in groups for template in group))


@dataclass(frozen=True)
class MetricFilterResolver:
    datasets: Mapping[str, OssieDataset]
    models: Mapping[str, SemanticModel]
    relationships: Sequence[OssieRelationship]
    custom_grains: Sequence[str]

    def resolve(
        self, templates: tuple[str, ...], dataset: str, reader: str
    ) -> tuple[Optional[str], bool]:
        related_filter = False

        def resolve_reference(
            kind: str, argument: str, entity_path: Sequence[str], time_modifier: Optional[str]
        ) -> str:
            owner, name = self._template_field(dataset, kind, argument, entity_path, time_modifier)
            return qualified_column(owner, name).sql(dialect=reader)

        parts: list[str] = []
        for template in templates:
            try:
                sql = _render_filter_template(template, resolve_reference).strip()
                if not sql:
                    continue
                tree = parse_scalar_sql(sql, reader)
            except (jinja2.TemplateError, TypeError, ValueError) as exc:
                raise UnresolvedFilterError(f"Could not render or parse the predicate ({exc})") from exc

            replacements: dict[tuple[int, int], str] = {}
            for column in tree.find_all(exp.Column):
                owner = column.table or dataset
                if column.db or column.catalog or owner not in self.datasets:
                    raise UnresolvedFilterError(f"Unknown filter dataset {owner!r}")
                if owner != dataset:
                    self._require_unique_relationship(dataset, owner)
                    related_filter = True
                name = self._field_name(owner, column.name, quoted=column.this.quoted)
                if column.table == owner and column.name == name:
                    continue
                span = column_span(column)
                if span is None:
                    raise UnresolvedFilterError(f"Could not locate filter field {name!r} in the SQL")
                replacements[span] = qualified_column(owner, name).sql(dialect=reader)
            try:
                parts.append(replace_columns_in_sql(sql, replacements))
            except ValueError as exc:
                raise UnresolvedFilterError(str(exc)) from exc

        parts = list(dict.fromkeys(parts))
        if len(parts) < 2:
            return (parts[0] if parts else None), related_filter
        return " AND ".join(f"({part})" for part in parts), related_filter

    def _template_field(
        self, dataset: str, kind: str, argument: str, entity_path: Sequence[str], time_modifier: Optional[str]
    ) -> tuple[str, str]:
        if kind == "Metric":
            raise UnresolvedFilterError(f"Metric filter {argument!r} cannot be evaluated at row level")
        if not isinstance(argument, str) or isinstance(entity_path, str):
            raise UnresolvedFilterError("Expected a field name and an optional list of entity names")
        if not all(isinstance(name, str) for name in entity_path):
            raise UnresolvedFilterError("Expected entity names in the filter path")
        try:
            structured = StructuredLinkableSpecName.from_name(
                argument.lower(), custom_granularity_names=self.custom_grains
            )
        except FeatureNotSupportedError as exc:
            raise UnresolvedFilterError(str(exc)) from exc
        if time_modifier or structured.time_granularity_name:
            raise UnresolvedFilterError(f"Time grain or date part in {argument!r} requires MetricFlow query context")

        name = structured.element_name
        links = [*(entity.lower() for entity in entity_path), *structured.entity_link_names]
        if links and (links[0] == self.models[dataset].primary_entity or self._is_key(dataset, links[0])):
            # The base model's primary or unique entity can anchor the path.
            if len(links) > 1 or self._declares(dataset, kind, name):
                links.pop(0)
        if len(links) > 1:
            raise UnresolvedFilterError(f"Entity path for {argument!r} requires multiple joins")
        if links:
            dataset = self._navigate_relationship(dataset, links[0])

        if not self._declares(dataset, kind, name):
            raise UnresolvedFilterError(f"{kind} {name!r} is not declared in {dataset!r}")
        return dataset, name

    def _declares(self, dataset: str, kind: str, name: str) -> bool:
        model = self.models[dataset]
        if kind == "Entity":
            return any(entity.name == name for entity in model.entities)
        return any(
            dimension.name == name and (kind == "Dimension" or dimension.type is DimensionType.TIME)
            for dimension in model.dimensions
        )

    def _field_name(self, dataset: str, name: str, quoted: bool = False) -> str:
        candidates = [
            field.name
            for field in self.datasets[dataset].fields or ()
            if field.name == name or (not quoted and field.name.casefold() == name.casefold())
        ]
        if len(candidates) != 1:
            raise UnresolvedFilterError(f"Filter field {dataset}.{name} does not identify one declared field")
        return candidates[0]

    def _is_key(self, dataset: str, entity: str) -> bool:
        return any(
            element.name == entity and element.type in (EntityType.PRIMARY, EntityType.UNIQUE)
            for element in self.models[dataset].entities
        )

    def _can_follow(self, dataset: str, relationship: OssieRelationship) -> bool:
        # The forward direction is many-to-one. Reverse is safe only if the
        # from-side dbt entity is also a key.
        return relationship.from_dataset == dataset or (
            relationship.to == dataset
            and self._is_key(relationship.from_dataset, relationship.name.rsplit("__", 1)[-1])
        )

    def _require_unique_relationship(self, source: str, target: str) -> None:
        links = [
            link for link in self.relationships if {link.from_dataset, link.to} == {source, target}
        ]
        if len(links) != 1 or not self._can_follow(source, links[0]):
            raise UnresolvedFilterError(f"No unique many-to-one relationship from {source!r} to {target!r}")

    def _navigate_relationship(self, dataset: str, entity: str) -> str:
        targets = [
            link.to if link.from_dataset == dataset else link.from_dataset
            for link in self.relationships
            if link.name.endswith(f"__{entity}") and self._can_follow(dataset, link)
        ]
        if len(targets) != 1:
            raise UnresolvedFilterError(f"Entity {entity!r} does not identify one relationship from {dataset!r}")
        return targets[0]
