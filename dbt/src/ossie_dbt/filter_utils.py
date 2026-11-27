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

from functools import partial
from typing import Mapping, Optional, Sequence, Tuple

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from metricflow_semantic_interfaces.protocols.semantic_model import SemanticModel
from metricflow_semantic_interfaces.protocols.where_filter import WhereFilterIntersection
from metricflow_semantic_interfaces.type_enums import DimensionType, EntityType


class _DimensionStub:
    """Jinja sandbox stub for `{{ Dimension('entity__dim') }}`.

    Renders to the qualified column name, e.g. `order__status`, when unresolved.
    Local references render to declared Ossie fields, e.g. `orders.status`.
    Method chaining (`grain`, `date_part`) appends a `__<suffix>` part.
    """

    def __init__(
        self, name: str, entity_path: Sequence[str] = (), *, local_fields: Optional[Mapping[str, str]] = None
    ) -> None:
        self._col = "__".join(list(entity_path) + [name])
        self._field = local_fields.get(self._col, self._col) if local_fields else self._col
        self._suffix = ""

    def grain(self, time_granularity: str) -> "_DimensionStub":
        self._suffix = f"__{time_granularity.lower()}"
        return self

    def date_part(self, date_part_name: str) -> "_DimensionStub":
        self._suffix = f"__{date_part_name.lower()}"
        return self

    def descending(self, _is_descending: bool) -> "_DimensionStub":
        return self

    def __str__(self) -> str:
        if self._suffix:
            return f"{self._col}{self._suffix}"
        return self._field


class _TimeDimensionStub:
    """Jinja sandbox stub for `{{ TimeDimension('entity__dim', 'grain') }}`.

    Renders to `entity__dim` or `entity__dim__grain` when a granularity is provided.
    Local references without a granularity can render to declared Ossie fields.
    """

    def __init__(
        self,
        name: str,
        time_granularity_name: Optional[str] = None,
        entity_path: Sequence[str] = (),
        *,
        local_fields: Optional[Mapping[str, str]] = None,
        **kwargs: object,
    ) -> None:
        self._col = "__".join(list(entity_path) + [name])
        self._field = self._col
        if local_fields and not kwargs.get("date_part_name"):
            self._field = local_fields.get(self._col, self._col)
        self._grain = time_granularity_name

    def grain(self, time_granularity: str) -> "_TimeDimensionStub":
        self._grain = time_granularity
        return self

    def date_part(self, date_part_name: str) -> "_TimeDimensionStub":
        self._grain = date_part_name
        return self

    def descending(self, _is_descending: bool) -> "_TimeDimensionStub":
        return self

    def __str__(self) -> str:
        if self._grain:
            return f"{self._col}__{self._grain.lower()}"
        return self._field


class _EntityStub:
    """Jinja sandbox stub for `{{ Entity('name') }}`."""

    def __init__(
        self, name: str, entity_path: Sequence[str] = (), *, local_fields: Optional[Mapping[str, str]] = None
    ) -> None:
        self._col = "__".join(list(entity_path) + [name])
        self._field = local_fields.get(self._col, self._col) if local_fields else self._col

    def descending(self, _is_descending: bool) -> "_EntityStub":
        return self

    def __str__(self) -> str:
        return self._field


class _MetricStub:
    """Jinja sandbox stub for `{{ Metric('name') }}`."""

    def __init__(self, name: str, group_by: Sequence[str] = ()) -> None:
        self._name = name

    def descending(self, _is_descending: bool) -> "_MetricStub":
        return self

    def __str__(self) -> str:
        return self._name


def _render_filter_template(template: str, model: Optional[SemanticModel] = None) -> str:
    """Render an MSI where-filter Jinja template to a plain SQL fragment.

    Jinja references such as `{{ Dimension('order__status') }}`,
    `{{ TimeDimension('order__ds', 'day') }}`, `{{ Entity('user') }}`,
    and `{{ Metric('revenue') }}` are resolved to their column-name
    equivalents using lightweight stubs. The output is a best-effort SQL
    string suitable for embedding in an Ossie expression.

    When the input metric's semantic model is known, local references such as
    `order__status` resolve to declared Ossie fields such as `orders.status`.

    Sandboxing is necessary to prevent SSTI-to-RCE style exploits
    """
    dimensions: Mapping[str, str] = {}
    time_dimensions: Mapping[str, str] = {}
    entities: Mapping[str, str] = {}

    if model is not None:
        local_entities = {e.name for e in model.entities if e.type in (EntityType.PRIMARY, EntityType.UNIQUE)}
        if model.primary_entity:
            local_entities.add(model.primary_entity)

        def local_references(names: Sequence[str]) -> Mapping[str, str]:
            # Ossie dataset fields use the declared MetricFlow names, even when backed by a different column.
            references = {name: f"{model.name}.{name}" for name in names}
            for entity in local_entities:
                references.update({f"{entity}__{name}": f"{model.name}.{name}" for name in names})
            return references

        dimensions = local_references([d.name for d in model.dimensions])
        time_dimensions = local_references([d.name for d in model.dimensions if d.type is DimensionType.TIME])
        entities = local_references([e.name for e in model.entities])

    return (
        SandboxedEnvironment(undefined=jinja2.StrictUndefined)
        .from_string(template)
        .render(
            Dimension=partial(_DimensionStub, local_fields=dimensions),
            TimeDimension=partial(_TimeDimensionStub, local_fields=time_dimensions),
            Entity=partial(_EntityStub, local_fields=entities),
            Metric=_MetricStub,
        )
    )


def _collect_filter_templates(filter_: Optional[WhereFilterIntersection]) -> Tuple[str, ...]:
    """Collect an MSI where-filter intersection as templates for later rendering."""
    if filter_ is None:
        return ()
    return tuple(part.where_sql_template for part in filter_.where_filters)


def _render_metric_filter(templates: Sequence[str], model: Optional[SemanticModel]) -> Optional[str]:
    """Render and merge MSI where-filter templates into a single SQL fragment."""
    rendered = (_render_filter_template(template, model).strip() for template in dict.fromkeys(templates))
    return _merge_filter_sqls(*rendered)


def _merge_filter_sqls(*parts: Optional[str]) -> Optional[str]:
    """Join non-None SQL filter strings with AND, wrapping each in parens when multiple.

    Repeated fragments are dropped. Filters are conjoined and AND is idempotent, so an
    input metric that restates a filter an enclosing metric already applies contributes
    nothing but ``(X) AND (X)`` noise — and would otherwise make two logically identical
    resolutions of the same reference compare unequal.
    """
    active = list(dict.fromkeys(p for p in parts if p))
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    return " AND ".join(f"({p})" for p in active)
