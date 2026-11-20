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

"""semantido SemanticLayer -> Apache Ossie document conversion.

Builds the Ossie model through the typed ``apache-ossie`` objects, so the
output is schema-conformant by construction. Governance metadata that has
no Ossie core field (privacy levels, SQL filters, time grain, the primary
time axis) is preserved losslessly in ``custom_extensions`` under the
``SEMANTIDO`` vendor name, with ``data`` as a serialized JSON string as
the specification requires.
"""

import json
import re
from typing import List, Optional

from ossie import (
    OssieAIContextObject,
    OssieCustomExtension,
    OssieDataset,
    OssieDataType,
    OssieDialect,
    OssieDialectExpression,
    OssieDimension,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieRelationship,
)
from semantido.generators.semantic_layer import (
    Column,
    Relationship,
    SemanticLayer,
    Table,
)

from ossie_semantido.constants import VENDOR_NAME
from ossie_semantido.converter_issues import (
    ConverterIssue,
    ConverterIssueType,
    ConverterResult,
)

_EQUALITY_RE = re.compile(r"^\s*(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)\s*$")
_AND_SPLIT_RE = re.compile(r"\s+AND\s+", re.IGNORECASE)


def _parse_join(
    join_condition: Optional[str], from_table: str
) -> Optional[tuple[list[str], list[str]]]:
    """Parse a (possibly composite) equi-join into aligned column arrays.

    Handles a single ``a.x = b.y`` as well as a composite
    ``a.x = b.x AND a.y = b.y`` (optionally parenthesized), orienting every
    pair so ``from_columns`` belongs to ``from_table`` regardless of which
    side of each equality it appeared on. Returns ``None`` if any conjunct is
    not a simple column equality or does not reference ``from_table``.
    """
    text = (join_condition or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    from_columns: list[str] = []
    to_columns: list[str] = []
    for part in _AND_SPLIT_RE.split(text):
        match = _EQUALITY_RE.match(part)
        if not match:
            return None
        left_table, left_col, right_table, right_col = match.groups()
        if left_table == from_table:
            from_columns.append(left_col)
            to_columns.append(right_col)
        elif right_table == from_table:
            from_columns.append(right_col)
            to_columns.append(left_col)
        else:
            return None
    if not from_columns:
        return None
    return from_columns, to_columns

# Maps a SQL base type (as semantido reports it, e.g. ``VARCHAR``,
# ``TIMESTAMP``) to Ossie's portable logical datatype. The exact SQL string
# is still kept verbatim in the SEMANTIDO extension so a round-trip is lossless.
_OSSIE_DATATYPE_BY_SQL = {
    "VARCHAR": OssieDataType.STRING,
    "CHAR": OssieDataType.STRING,
    "TEXT": OssieDataType.STRING,
    "INTEGER": OssieDataType.INTEGER,
    "INT": OssieDataType.INTEGER,
    "BIGINT": OssieDataType.INTEGER,
    "SMALLINT": OssieDataType.INTEGER,
    "NUMERIC": OssieDataType.DECIMAL,
    "DECIMAL": OssieDataType.DECIMAL,
    "FLOAT": OssieDataType.FLOAT,
    "REAL": OssieDataType.FLOAT,
    "DOUBLE": OssieDataType.FLOAT,
    "BOOLEAN": OssieDataType.BOOLEAN,
    "BOOL": OssieDataType.BOOLEAN,
    "DATE": OssieDataType.DATE,
    "TIME": OssieDataType.TIME,
    "DATETIME": OssieDataType.DATE_TIME,
    "TIMESTAMP": OssieDataType.DATE_TIME,
}


def _ossie_datatype(data_type: Optional[str]) -> Optional[OssieDataType]:
    """Map a semantido SQL type string to a portable Ossie datatype, if known."""
    if not data_type:
        return None
    base = data_type.split("(")[0].strip().upper()
    return _OSSIE_DATATYPE_BY_SQL.get(base)


def _extension(payload: dict) -> OssieCustomExtension:
    """Wrap semantido-specific metadata as a spec-conformant custom extension."""
    return OssieCustomExtension(
        vendor_name=VENDOR_NAME,
        data=json.dumps(payload, sort_keys=True, default=str),
    )


def _ansi(expression: str) -> OssieExpression:
    return OssieExpression(
        dialects=[
            OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=expression)
        ]
    )


def _column_to_field(column: Column) -> OssieField:
    ai_kwargs = {}
    if column.synonyms:
        ai_kwargs["synonyms"] = tuple(column.synonyms)
    if column.sample_values:
        ai_kwargs["examples"] = tuple(str(v) for v in column.sample_values)
    if column.application_rules:
        ai_kwargs["instructions"] = " ".join(column.application_rules)

    extension_payload = {}
    if column.privacy_level is not None:
        extension_payload["privacy_level"] = column.privacy_level.value
    if column.time_grain is not None:
        extension_payload["time_grain"] = column.time_grain.value
    if column.data_type:
        extension_payload["data_type"] = column.data_type
    if column.references:
        extension_payload["references"] = column.references
    if column.concept:  # semantido v0.4.0+
        extension_payload["concept"] = column.concept
    if column.application_rules:
        # Also joined into ai_context.instructions above for Ossie-native
        # consumers; kept verbatim here so the reverse direction can
        # regenerate the authored list.
        extension_payload["application_rules"] = list(column.application_rules)

    return OssieField(
        name=column.name,
        expression=_ansi(column.name),
        dimension=OssieDimension(is_time=True) if column.is_time_dimension else None,
        datatype=_ossie_datatype(column.data_type),
        description=column.description or None,
        ai_context=OssieAIContextObject(**ai_kwargs) if ai_kwargs else None,
        custom_extensions=[_extension(extension_payload)]
        if extension_payload
        else None,
    )


def _table_to_dataset(table: Table) -> OssieDataset:
    instructions_parts = [
        p for p in (table.business_context, table.application_context) if p
    ]

    ai_kwargs = {}
    if instructions_parts:
        ai_kwargs["instructions"] = " ".join(instructions_parts)
    if table.synonyms:
        ai_kwargs["synonyms"] = tuple(table.synonyms)

    extension_payload = {}
    if table.sql_filters:
        extension_payload["sql_filters"] = table.sql_filters
    if table.time_dimension:
        extension_payload["time_dimension"] = table.time_dimension
    if table.concept:  # semantido v0.4.0+
        extension_payload["concept"] = table.concept
    if table.unique_keys:  # semantido v0.5.0+
        extension_payload["unique_keys"] = [list(k) for k in table.unique_keys]
    # ai_context.instructions joins business + application context for
    # Ossie-native consumers; the split is preserved here, so the reverse
    # direction regenerates the authored attributes rather than merging.
    if table.business_context:
        extension_payload["business_context"] = table.business_context
    if table.application_context:
        extension_payload["application_context"] = table.application_context

    return OssieDataset(
        name=table.name,
        source=f"{table.schema}.{table.name}" if table.schema else table.name,
        primary_key=table.primary_key if table.primary_key else None,
        description=table.description or None,
        ai_context=OssieAIContextObject(**ai_kwargs) if ai_kwargs else None,
        fields=[_column_to_field(c) for c in table.columns] or None,
        custom_extensions=[_extension(extension_payload)]
        if extension_payload
        else None,
    )


def _relationship_to_ossie(
    rel: Relationship, issues: List[ConverterIssue]
) -> Optional[OssieRelationship]:
    parsed = _parse_join(rel.join_condition, rel.from_table)
    if parsed is None:
        # Unparseable (aliases, functions, non-equi) — report rather than
        # silently swapping or losing columns.
        issues.append(
            ConverterIssue(
                issue_type=ConverterIssueType.RELATIONSHIP_JOIN_UNPARSED,
                element_name=f"{rel.from_table}->{rel.to_table}",
            )
        )
        return None

    from_columns, to_columns = parsed

    ai_kwargs = {}
    if rel.description:
        ai_kwargs["instructions"] = rel.description

    # Include the FK columns so two relationships between the same pair of
    # tables (e.g. buyer_id and seller_id both -> party), and each column of a
    # composite key, get distinct names — the duplicate-name validation would
    # otherwise reject the document.
    name = f"{rel.from_table}__{rel.to_table}__" + "_".join(from_columns)

    return OssieRelationship.model_validate(
        {
            "name": name,
            "from": rel.from_table,
            "to": rel.to_table,
            "from_columns": from_columns,
            "to_columns": to_columns,
            "ai_context": ai_kwargs or None,
            "custom_extensions": [
                _extension(
                    {"relationship_type": rel.relationship_type.value}
                ).model_dump()
            ],
        }
    )


def semantic_layer_to_ossie(
    layer: SemanticLayer, model_name: str, description: Optional[str] = None
) -> ConverterResult[OssieDocument]:
    """Convert a synced semantido SemanticLayer into an Ossie document."""
    issues: List[ConverterIssue] = []

    datasets = [_table_to_dataset(t) for t in layer.tables.values()]

    relationships = []
    for rel in layer.relationships:
        converted = _relationship_to_ossie(rel, issues)
        if converted is not None:
            relationships.append(converted)

    model_payload = {}
    if layer.application_glossary:
        model_payload["application_glossary"] = layer.application_glossary
    if layer.concept_registry is not None:  # semantido v0.4.0+
        # The registry is the meaning artifact: definitions, relations,
        # external mappings, and grain (v0.5.0). Serialized whole so a
        # round-trip regenerates the authoring code.
        model_payload["concept_registry"] = layer.concept_registry.to_dict()
    model_extensions = [_extension(model_payload)] if model_payload else None

    # OssieDocument is itself a single semantic model: datasets,
    # relationships, metrics, and custom_extensions live at the root.
    document = OssieDocument(
        name=model_name,
        description=description,
        datasets=datasets,
        relationships=relationships or None,
        custom_extensions=model_extensions,
    )
    return ConverterResult(output=document, issues=issues)
