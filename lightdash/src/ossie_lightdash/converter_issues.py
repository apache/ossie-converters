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
from dataclasses import dataclass
from enum import Enum
from typing import Generic, List, TypeVar


class ConverterIssueType(Enum):
    """Identifies the kind of information loss that occurred during conversion.

    Each member's comment names the direction (import or export) and the
    construct that has no equivalent on the other side.
    """

    # Import: no `--schema`, so the dataset source has no schema or database.
    SOURCE_UNQUALIFIED = "SOURCE_UNQUALIFIED"
    # Import: the join's `sql_on` yields no column pair (an expression join).
    # The join is kept in the dataset's extension and produces no relationship.
    JOIN_SQL_UNPARSED = "JOIN_SQL_UNPARSED"
    # Import: Ossie relationships can't reproduce the join exactly (chained
    # through another joined model, or extra conditions). The join is kept
    # verbatim in the dataset's extension; its column pairs still become
    # relationships.
    JOIN_STASHED = "JOIN_STASHED"
    # Export: the metric references more than one dataset, and no Lightdash
    # model metric can express it.
    CROSS_DATASET_METRIC_DROPPED = "CROSS_DATASET_METRIC_DROPPED"
    # Export: the relationship's `from_columns` and `to_columns` differ in
    # length, so no correct `sql_on` exists.
    RELATIONSHIP_COLUMNS_MISMATCHED = "RELATIONSHIP_COLUMNS_MISMATCHED"
    # Export: the `LIGHTDASH` extension's data is not valid JSON. Its
    # presentation attributes are lost.
    EXTENSION_DATA_INVALID = "EXTENSION_DATA_INVALID"
    # Import: the model-level metric has no `sql`, so it has no Ossie
    # expression. The metric is skipped.
    METRIC_SQL_MISSING = "METRIC_SQL_MISSING"
    # Export: the field has a time-axis role (`dimension.is_time`) that its
    # datatype does not imply. Lightdash has no equivalent marker.
    TIME_ROLE_NOT_REPRESENTABLE = "TIME_ROLE_NOT_REPRESENTABLE"
    # Export: the custom extension belongs to another vendor. It stays in the
    # Ossie document and is not written to Lightdash meta.
    FOREIGN_EXTENSION_IGNORED = "FOREIGN_EXTENSION_IGNORED"
    # Import: the expression references project parameters or user attributes
    # (`${lightdash.parameters.x}`, `${ld.user.x}`), which have no Ossie form.
    # The element is skipped.
    EXPRESSION_NOT_PORTABLE = "EXPRESSION_NOT_PORTABLE"
    # Import: a `${metric}` reference is replaced by that metric's expression,
    # because Ossie metrics can't reference each other.
    METRIC_REFERENCE_INLINED = "METRIC_REFERENCE_INLINED"
    # Import: a `${alias.column}` reference to an aliased join is rewritten to
    # the joined dataset. Ossie has no join aliases, so the join path is lost.
    ALIAS_REFERENCE_FLATTENED = "ALIAS_REFERENCE_FLATTENED"
    # Export: the expression offers neither the requested dialect nor a
    # portable one. The first dialect offered is used.
    DIALECT_UNAVAILABLE = "DIALECT_UNAVAILABLE"
    # Export: the field expression references a dataset that the field's
    # dataset does not join, so Lightdash can't resolve the reference.
    FIELD_REFERENCE_UNJOINED = "FIELD_REFERENCE_UNJOINED"
    # Import: two metrics share a name after qualification with their model
    # name. The later one gets a suffix.
    METRIC_NAME_COLLISION = "METRIC_NAME_COLLISION"
    # Import: the metric's `filters` are kept for Lightdash only. The Ossie
    # expression is unfiltered, so other consumers compute a different number.
    METRIC_FILTER_NOT_PORTABLE = "METRIC_FILTER_NOT_PORTABLE"
    # Import: the model's `sql_filter`, `sql_where`, or `required_filters` are
    # kept for Lightdash only. The Ossie dataset is unrestricted for other
    # consumers.
    ROW_FILTER_NOT_PORTABLE = "ROW_FILTER_NOT_PORTABLE"
    # Export (model file): a dimension needs a type and the field has no
    # datatype, so the dimension gets `string`.
    DIMENSION_TYPE_DEFAULTED = "DIMENSION_TYPE_DEFAULTED"
    # Export (model file): column meta other than `additional_dimensions` has
    # no place on a model-file dimension.
    COLUMN_META_NOT_REPRESENTABLE = "COLUMN_META_NOT_REPRESENTABLE"
    # Import: `--catalog` has no entry for the model, so its columns get no
    # types from the catalog.
    CATALOG_MODEL_MISSING = "CATALOG_MODEL_MISSING"
    # Import: the join targets a model that is not in the input. The
    # relationship would reference an unknown dataset, so it is skipped.
    JOIN_TARGET_UNKNOWN = "JOIN_TARGET_UNKNOWN"
    # Import: the SQL can't be parsed. Explicit references are converted, and
    # bare column names stay as written.
    COLUMN_QUALIFICATION_SKIPPED = "COLUMN_QUALIFICATION_SKIPPED"


# One line per issue type, printed by the CLI under the issue's header.
ISSUE_EXPLANATIONS = {
    ConverterIssueType.COLUMN_QUALIFICATION_SKIPPED: "SQL can't be parsed; bare column names left as written",
    ConverterIssueType.SOURCE_UNQUALIFIED: "no --schema given, so the dataset source is only the model name",
    ConverterIssueType.JOIN_SQL_UNPARSED: "no column pair in sql_on; kept for Lightdash only, no relationship",
    ConverterIssueType.JOIN_STASHED: "chained join, expression join or extra conditions; kept verbatim for Lightdash, relationships derived from the column pairs",
    ConverterIssueType.CROSS_DATASET_METRIC_DROPPED: "no single model joins every dataset the expression references",
    ConverterIssueType.RELATIONSHIP_COLUMNS_MISMATCHED: "from_columns and to_columns differ in length; skipped",
    ConverterIssueType.EXTENSION_DATA_INVALID: "LIGHTDASH extension data is not valid JSON; its attributes are lost",
    ConverterIssueType.METRIC_SQL_MISSING: "model-level metric without sql; skipped",
    ConverterIssueType.TIME_ROLE_NOT_REPRESENTABLE: "is_time on a non-date type (for example, an integer year); Lightdash has no such marker, so the column is a plain dimension",
    ConverterIssueType.FOREIGN_EXTENSION_IGNORED: "another vendor's extension; left untouched in the Ossie document",
    ConverterIssueType.EXPRESSION_NOT_PORTABLE: "SQL uses parameters, user attributes or Liquid, which only Lightdash can evaluate; skipped",
    ConverterIssueType.METRIC_REFERENCE_INLINED: "${metric} reference replaced by that metric's expression",
    ConverterIssueType.ALIAS_REFERENCE_FLATTENED: "${alias.column} now points at the joined model; which join was meant is lost",
    ConverterIssueType.DIALECT_UNAVAILABLE: "neither --dialect, OSSIE_SQL_2026 nor ANSI_SQL offered; first available dialect used",
    ConverterIssueType.FIELD_REFERENCE_UNJOINED: "expression names a dataset this model does not join; emitted as is",
    ConverterIssueType.METRIC_NAME_COLLISION: "still a duplicate after <model>_<metric>; suffixed",
    ConverterIssueType.METRIC_FILTER_NOT_PORTABLE: "filters kept for Lightdash only; other tools see the unfiltered aggregate",
    ConverterIssueType.ROW_FILTER_NOT_PORTABLE: "sql_filter / required_filters kept for Lightdash only; other tools see all rows",
    ConverterIssueType.DIMENSION_TYPE_DEFAULTED: "no datatype on the field; dimension type set to string",
    ConverterIssueType.COLUMN_META_NOT_REPRESENTABLE: "column meta other than additional_dimensions has no place on a model-file dimension; dropped",
    ConverterIssueType.JOIN_TARGET_UNKNOWN: "join to a model that is not in the input; skipped",
    ConverterIssueType.CATALOG_MODEL_MISSING: "not in the catalog (stale, or never built); its columns get no types from it",
}


class ConversionError(ValueError):
    """Signals input that can't be converted at all.

    A ``ConverterIssue`` records a loss in an otherwise successful conversion.
    ``ConversionError`` is raised for input that is not the shape the
    converter reads, and its message names the element. The CLI prints the
    message and exits with code 2.
    """


@dataclass(frozen=True)
class ConverterIssue:
    """Records a single instance of information loss during conversion."""

    issue_type: ConverterIssueType
    element_name: str


T = TypeVar("T")


@dataclass(frozen=True)
class ConverterResult(Generic[T]):
    """Pairs a converter's output with the issues raised while producing it."""

    output: T
    issues: List[ConverterIssue]
