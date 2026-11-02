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

"""
Internal data models for the Ossie <-> Denodo Metric View converters.

These dataclasses represent the parsed intermediate representation of
VQL Association and Metric View statements, decoupling parsing from rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Association models
# ---------------------------------------------------------------------------

@dataclass
class EndpointModel:
    """Represents one ENDPOINT entry inside a CREATE ASSOCIATION statement."""

    role_name: str
    view_name: str
    is_principal: bool
    cardinality: str


@dataclass
class AssociationModel:
    """Represents a complete CREATE [OR REPLACE] ASSOCIATION VQL statement."""

    name: str
    vdb: str | None
    folder: str | None
    referential_constraint: bool
    endpoints: list[EndpointModel] = field(default_factory=list)
    mappings: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Metric View models
# ---------------------------------------------------------------------------

@dataclass
class SourceModel:
    """Represents one entry in the SOURCES clause of a Metric View."""

    alias: str
    database: str | None
    view: str


@dataclass
class AssociationRefModel:
    """Represents one entry in the ASSOCIATIONS clause of a Metric View."""

    from_alias: str
    to_alias: str
    association_name: str
    join_type: str | None


@dataclass
class DimensionFieldModel:
    """Represents one field inside a DIMENSIONS group or at the top level."""

    name: str
    expression: str


@dataclass
class DimensionGroupModel:
    """Represents a named group or an ungrouped field in the DIMENSIONS clause."""

    group_name: str | None
    fields: list[DimensionFieldModel] = field(default_factory=list)


@dataclass
class MetricFieldModel:
    """Represents one entry in the METRICS clause of a Metric View."""

    name: str
    expression: str


@dataclass
class MetricViewModel:
    """Represents a complete CREATE [OR REPLACE] METRIC VIEW VQL statement."""

    name: str
    folder: str | None
    description: str | None
    filter_condition: str | None
    tags: list[str] = field(default_factory=list)  # populated from DENODO custom_extension
    sources: list[SourceModel] = field(default_factory=list)
    associations: list[AssociationRefModel] = field(default_factory=list)
    dimensions: list[DimensionGroupModel] = field(default_factory=list)
    metrics: list[MetricFieldModel] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)
