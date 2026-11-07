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

"""Mapping between CDS built-in types and Ossie logical data types."""

from typing import Any, Optional

# CDS built-ins, plus the SAP HANA-specific ones CSN spells `cds.hana.*`.
_CDS_TO_OSSIE: dict[str, str] = {
    "cds.String": "String",
    "cds.LargeString": "String",
    "cds.UUID": "String",
    "cds.hana.CHAR": "String",
    "cds.hana.NCHAR": "String",
    "cds.hana.VARCHAR": "String",
    "cds.hana.CLOB": "String",
    "cds.Integer": "Integer",
    "cds.Int16": "Integer",
    "cds.Int32": "Integer",
    "cds.Int64": "Integer",
    "cds.Integer64": "Integer",
    "cds.UInt8": "Integer",
    "cds.hana.SMALLINT": "Integer",
    "cds.hana.TINYINT": "Integer",
    "cds.Decimal": "Decimal",
    "cds.DecimalFloat": "Decimal",
    "cds.hana.SMALLDECIMAL": "Decimal",
    "cds.Double": "Float",
    "cds.hana.REAL": "Float",
    "cds.Boolean": "Boolean",
    "cds.Date": "Date",
    "cds.Time": "Time",
    "cds.DateTime": "DateTime",
    "cds.Timestamp": "DateTime",
}

# Types with no portable Ossie equivalent: carried as Opaque.
_CDS_OPAQUE = frozenset(
    {
        "cds.Binary",
        "cds.LargeBinary",
        "cds.Vector",
        "cds.hana.BINARY",
        "cds.hana.ST_POINT",
        "cds.hana.ST_GEOMETRY",
    }
)

_OSSIE_TO_CDS: dict[str, str] = {
    "String": "cds.String",
    "Integer": "cds.Integer",
    "Decimal": "cds.Decimal",
    "Float": "cds.Double",
    "Boolean": "cds.Boolean",
    "Date": "cds.Date",
    "Time": "cds.Time",
    "DateTime": "cds.Timestamp",
    "DateTimeTz": "cds.Timestamp",
}

ASSOCIATION_TYPES = frozenset({"cds.Association", "cds.Composition"})


def _referenced_type(ref: Any, definitions: dict[str, Any]) -> Any:
    """The ``type`` of the element a ``type of`` reference (``["Books", "title"]``) names."""
    if not (isinstance(ref, list) and len(ref) >= 2 and all(isinstance(s, str) for s in ref)):
        return None
    node = definitions.get(ref[0])
    for segment in ref[1:]:
        elements = node.get("elements") if isinstance(node, dict) else None
        node = elements.get(segment) if isinstance(elements, dict) else None
    return node.get("type") if isinstance(node, dict) else None


def builtin_type(type_name: Any, definitions: dict[str, Any]) -> Optional[str]:
    """Resolve a (possibly user-defined) CSN type down to a CDS built-in.

    ``type_name`` is a type name, or a ``type of`` reference written ``{"ref": [...]}``.
    Returns ``None`` when the chain ends in something that is not a known built-in
    (an unknown name, a structured type, or a cycle).
    """
    seen: list[Any] = []
    while type_name is not None and type_name not in seen:
        seen.append(type_name)
        if isinstance(type_name, dict):
            type_name = _referenced_type(type_name.get("ref"), definitions)
            continue
        if not isinstance(type_name, str):
            return None
        if type_name in _CDS_TO_OSSIE or type_name in _CDS_OPAQUE or type_name in ASSOCIATION_TYPES:
            return type_name
        definition = definitions.get(type_name)
        if not isinstance(definition, dict):
            return None
        type_name = definition.get("type")
    return None


def ossie_datatype(builtin: Optional[str]) -> Optional[str]:
    """Ossie datatype for a CDS built-in, ``Opaque`` for a known non-portable one, else ``None``."""
    if builtin is None:
        return None
    if builtin in _CDS_TO_OSSIE:
        return _CDS_TO_OSSIE[builtin]
    if builtin in _CDS_OPAQUE:
        return "Opaque"
    return None


def cds_type(datatype: Optional[str]) -> Optional[str]:
    """CDS built-in for an Ossie datatype (``None`` for ``Opaque`` or an unset type)."""
    return _OSSIE_TO_CDS.get(datatype) if datatype else None


# The SQL type a CXN `cast` renders as. Each one reads back as the CDS type it came
# from (SQL_TYPE_TO_CDS), so a cast survives the trip through SQL. The one exception is
# cds.Int32, which CDS defines as another name for cds.Integer; the importer keeps the
# original CXN in the SAP_CSN extension so that spelling survives too.
CDS_TO_SQL_TYPE: dict[str, str] = {
    "cds.String": "VARCHAR",
    "cds.LargeString": "CLOB",
    "cds.Integer": "INT",
    "cds.Int16": "SMALLINT",
    "cds.Int32": "INT",
    "cds.Int64": "BIGINT",
    "cds.Decimal": "DECIMAL",
    "cds.Double": "DOUBLE",
    "cds.Boolean": "BOOLEAN",
    "cds.Date": "DATE",
    "cds.Time": "TIME",
    "cds.DateTime": "TIMESTAMP(0)",  # seconds precision
    "cds.Timestamp": "TIMESTAMP",  # fractional seconds
}

# A sqlglot type name (with its parameters, see cxn) back to its CDS type.
SQL_TYPE_TO_CDS: dict[str, str] = {
    "VARCHAR": "cds.String",
    "TEXT": "cds.LargeString",  # sqlglot reads CLOB as TEXT
    "INT": "cds.Integer",
    "SMALLINT": "cds.Int16",
    "BIGINT": "cds.Int64",
    "DECIMAL": "cds.Decimal",
    "DOUBLE": "cds.Double",
    "BOOLEAN": "cds.Boolean",
    "DATE": "cds.Date",
    "TIME": "cds.Time",
    "TIMESTAMP(0)": "cds.DateTime",
    "TIMESTAMP": "cds.Timestamp",
}

# Type facets a `cast` carries, written as the SQL type's parameters in this order.
CAST_FACETS: dict[str, tuple[str, ...]] = {
    "cds.String": ("length",),
    "cds.Decimal": ("precision", "scale"),
}
