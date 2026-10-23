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

"""Warehouse type vocabulary handling for the Solid converter.

Solid stores a column's `type` as the **raw** warehouse type string, verbatim from the
catalog -- `NUMBER(38,0)`, `TEXT`, `INT64`, `LONG` -- with no normalization. Two things
follow from that:

1. The vocabulary identifies the warehouse, which is how `infer_dialect` recovers a
   dialect that the Solid YAML itself never records (see README, "Dialect resolution").
2. Mapping it onto Apache Ossie's portable `datatype` enum is lossy (`NUMBER(38,2)`,
   `NUMERIC` and `DECIMAL` all collapse to `Decimal`), so the raw string is always kept
   in the SOLID stash and is what an export re-emits.
"""

import re

from ._common import (
    DIALECT_ANSI,
    DIALECT_BIGQUERY,
    DIALECT_DATABRICKS,
    DIALECT_SNOWFLAKE,
    SUPPORTED_DIALECTS,
    ConversionError,
    warn,
)

# Apache Ossie portable logical data types (core-spec/ossie-schema.json $defs.DataType).
STRING = "String"
INTEGER = "Integer"
DECIMAL = "Decimal"
FLOAT = "Float"
BOOLEAN = "Boolean"
DATE = "Date"
TIME = "Time"
DATE_TIME = "DateTime"
DATE_TIME_TZ = "DateTimeTz"
OPAQUE = "Opaque"

TEMPORAL_DATATYPES = frozenset({DATE, TIME, DATE_TIME, DATE_TIME_TZ})
OSSIE_DATATYPES = frozenset({
    STRING, INTEGER, DECIMAL, FLOAT, BOOLEAN, DATE, TIME, DATE_TIME, DATE_TIME_TZ, OPAQUE,
})

# Type names that, among the three warehouses with an Apache Ossie dialect, only one
# reports, and so identify it. Names shared across them are deliberately excluded -- no
# signal: STRING is both Databricks and BigQuery; STRUCT likewise, since `parse_type`
# strips the angle-bracket params that would tell them apart; TIMESTAMP_NTZ is both
# Snowflake and Databricks; VARIANT, GEOGRAPHY and GEOMETRY are Databricks types now
# too, and GEOGRAPHY a BigQuery one; BOOLEAN, DATE, FLOAT and DECIMAL are near-universal.
_DIALECT_MARKERS = {
    DIALECT_SNOWFLAKE: frozenset(
        {"NUMBER", "TEXT", "OBJECT", "TIMESTAMP_LTZ", "TIMESTAMP_TZ"}
    ),
    DIALECT_DATABRICKS: frozenset(
        {"LONG", "MAP", "BIGINT", "TINYINT", "SMALLINT", "BYTE", "SHORT", "VOID"}
    ),
    DIALECT_BIGQUERY: frozenset(
        {"INT64", "FLOAT64", "BOOL", "BIGNUMERIC", "BIGDECIMAL", "BYTES", "RECORD"}
    ),
}

# Type names only warehouses with no Apache Ossie dialect of their own report -- Oracle
# and SQL Server, both Solid sources. Oracle's NUMBER would otherwise read as Snowflake
# and SQL Server's BIGINT as Databricks, labelling every expression with a dialect it
# was not written in, so any of these sends inference to ANSI_SQL with a warning.
_OTHER_WAREHOUSE_MARKERS = frozenset({
    "VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB",                    # Oracle
    "DATETIME2", "DATETIMEOFFSET", "SMALLDATETIME", "UNIQUEIDENTIFIER", "MONEY",
    "SMALLMONEY", "NTEXT", "BIT",                                 # SQL Server
})

# Warehouse type -> Apache Ossie datatype, for names that mean the same thing
# everywhere. Dialect-sensitive names (the TIMESTAMP family, NUMBER/NUMERIC scale) are
# resolved in `to_ossie_datatype` instead.
_COMMON_TYPES = {
    # character
    "STRING": STRING, "TEXT": STRING, "VARCHAR": STRING, "CHAR": STRING,
    "CHARACTER": STRING, "NVARCHAR": STRING, "NCHAR": STRING, "STRING_TYPE": STRING,
    "CHARACTER VARYING": STRING, "CHAR VARYING": STRING, "VARCHAR2": STRING,
    "NVARCHAR2": STRING, "CLOB": STRING, "NCLOB": STRING, "NTEXT": STRING,
    "UNIQUEIDENTIFIER": STRING,
    # exact integral
    "INT": INTEGER, "INTEGER": INTEGER, "BIGINT": INTEGER, "SMALLINT": INTEGER,
    "TINYINT": INTEGER, "BYTEINT": INTEGER, "LONG": INTEGER, "SHORT": INTEGER,
    "BYTE": INTEGER, "INT64": INTEGER, "INT2": INTEGER, "INT4": INTEGER,
    "INT8": INTEGER, "SERIAL": INTEGER,
    # approximate
    "FLOAT": FLOAT, "FLOAT4": FLOAT, "FLOAT8": FLOAT, "FLOAT64": FLOAT,
    "DOUBLE": FLOAT, "DOUBLE PRECISION": FLOAT, "REAL": FLOAT,
    # boolean
    "BOOLEAN": BOOLEAN, "BOOL": BOOLEAN, "BIT": BOOLEAN,
    # exact, fixed-scale money
    "MONEY": DECIMAL, "SMALLMONEY": DECIMAL,
    # temporal (dialect-independent)
    "DATE": DATE, "TIME": TIME,
    # not representable in the portable vocabulary
    "VARIANT": OPAQUE, "OBJECT": OPAQUE, "ARRAY": OPAQUE, "MAP": OPAQUE,
    "STRUCT": OPAQUE, "RECORD": OPAQUE, "JSON": OPAQUE, "XML": OPAQUE,
    "BINARY": OPAQUE, "VARBINARY": OPAQUE, "BYTES": OPAQUE, "GEOGRAPHY": OPAQUE,
    "GEOMETRY": OPAQUE, "INTERVAL": OPAQUE, "VOID": OPAQUE, "NULL": OPAQUE,
    "VECTOR": OPAQUE, "RANGE": OPAQUE,
}

# Exact-decimal families. One is an Integer only when its scale is known to be 0: a
# declared scale of 0 (`NUMBER(38,0)`), or a precision alone (`NUMERIC(10)`), which
# declares a scale of 0 in every warehouse.
#
# A bare name is a Decimal -- "exact base-10 with unspecified precision and scale", as
# the spec defines it -- even where the warehouse's own default scale is 0. Solid copies
# a column's type from the catalog's DATA_TYPE, which for Snowflake and Databricks never
# carries the precision or scale (those are separate catalog columns), so a bare NUMBER
# in a Solid export is as likely NUMBER(38,2) as NUMBER(38,0): every one of the 151
# NUMBER and DECIMAL columns in the Solid exports this was checked against is bare,
# including rates and dollar amounts. BigQuery's bare NUMERIC is (38,9) and Oracle's
# bare NUMBER is a floating decimal, both Decimals anyway.
_DECIMAL_TYPES = frozenset(
    {"NUMBER", "NUMERIC", "DECIMAL", "DEC", "BIGNUMERIC", "BIGDECIMAL"})

# TIMESTAMP means different things per warehouse: BigQuery's and Databricks' are
# instants (offset-aware), Snowflake's bare TIMESTAMP is an alias for TIMESTAMP_NTZ.
_TIMESTAMP_BY_DIALECT = {
    DIALECT_SNOWFLAKE: DATE_TIME,
    DIALECT_DATABRICKS: DATE_TIME_TZ,
    DIALECT_BIGQUERY: DATE_TIME_TZ,
    DIALECT_ANSI: DATE_TIME,
}

_TIMESTAMP_VARIANTS = {
    "TIMESTAMP_NTZ": DATE_TIME,
    "TIMESTAMPNTZ": DATE_TIME,
    "DATETIME": DATE_TIME,
    "DATETIME2": DATE_TIME,
    "SMALLDATETIME": DATE_TIME,
    "DATETIMEOFFSET": DATE_TIME_TZ,
    "TIMESTAMP WITH LOCAL TIME ZONE": DATE_TIME_TZ,
    "TIMESTAMP_TZ": DATE_TIME_TZ,
    "TIMESTAMPTZ": DATE_TIME_TZ,
    "TIMESTAMP_LTZ": DATE_TIME_TZ,
    "TIMESTAMPLTZ": DATE_TIME_TZ,
    "TIMESTAMP WITH TIME ZONE": DATE_TIME_TZ,
    "TIMESTAMP WITHOUT TIME ZONE": DATE_TIME,
    "TIMESTAMP_UNSPECIFIED": DATE_TIME,
}

# Apache Ossie datatype -> the type name an export writes back when the original raw
# string is unavailable (a hand-authored Apache Ossie model with no SOLID stash). Solid
# reads it as the column's catalog type, so each one is a type of that warehouse that
# maps back to the same datatype, with the scale written out, since a bare NUMBER says
# nothing about it. The one exception is Time on
# Databricks, which has no time-of-day type; it is written as STRING and reads back as
# String (see TYPE_ROUND_TRIP_EXCEPTIONS).
_DATATYPE_TO_RAW = {
    DIALECT_SNOWFLAKE: {
        STRING: "TEXT", INTEGER: "NUMBER(38,0)", DECIMAL: "NUMBER(38,9)", FLOAT: "FLOAT",
        BOOLEAN: "BOOLEAN", DATE: "DATE", TIME: "TIME", DATE_TIME: "TIMESTAMP_NTZ",
        DATE_TIME_TZ: "TIMESTAMP_TZ", OPAQUE: "VARIANT",
    },
    DIALECT_DATABRICKS: {
        STRING: "STRING", INTEGER: "LONG", DECIMAL: "DECIMAL(38,9)", FLOAT: "DOUBLE",
        BOOLEAN: "BOOLEAN", DATE: "DATE", TIME: "STRING", DATE_TIME: "TIMESTAMP_NTZ",
        DATE_TIME_TZ: "TIMESTAMP", OPAQUE: "VARIANT",
    },
    DIALECT_BIGQUERY: {
        STRING: "STRING", INTEGER: "INT64", DECIMAL: "NUMERIC", FLOAT: "FLOAT64",
        BOOLEAN: "BOOL", DATE: "DATE", TIME: "TIME", DATE_TIME: "DATETIME",
        DATE_TIME_TZ: "TIMESTAMP", OPAQUE: "JSON",
    },
    DIALECT_ANSI: {
        STRING: "VARCHAR", INTEGER: "INTEGER", DECIMAL: "DECIMAL(38,9)", FLOAT: "DOUBLE",
        BOOLEAN: "BOOLEAN", DATE: "DATE", TIME: "TIME", DATE_TIME: "TIMESTAMP",
        DATE_TIME_TZ: "TIMESTAMP WITH TIME ZONE", OPAQUE: "JSON",
    },
}

# (dialect, datatype) pairs that do not survive to_raw_type -> to_ossie_datatype, each
# because the warehouse has no type for it.
TYPE_ROUND_TRIP_EXCEPTIONS = {(DIALECT_DATABRICKS, TIME): STRING}

# Leading type name, with any parameter list (`NUMBER(38,2)`, `VARCHAR(16777216)`) and
# any element type (`ARRAY<STRING>`, `MAP<STRING,INT>`) captured separately.
_TYPE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*(?:\(([^)]*)\)|<.*>)?\s*$")


def parse_type(raw):
    """Split a raw warehouse type into (BASE_NAME, params) with BASE_NAME upper-cased.

    `NUMBER(38,2)` -> ("NUMBER", ["38", "2"]); `ARRAY<STRING>` -> ("ARRAY", []);
    an unparseable or empty value -> (None, []).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, []
    match = _TYPE_RE.match(raw)
    if not match:
        return raw.strip().upper(), []
    base = " ".join(match.group(1).split()).upper()
    params = [p.strip() for p in (match.group(2) or "").split(",") if p.strip()]
    return base or None, params


def infer_dialect(raw_types):
    """Infer the Apache Ossie dialect from a model's column type vocabulary.

    Solid's YAML export does not record the source warehouse -- it lives on the asset
    row in Solid's own database and is dropped at render time -- but the raw type names
    it copies out of the catalog do identify it. Each distinctive name votes for its
    warehouse; the warehouse with the most votes wins.

    Returns (dialect, confident). `confident` is False when no marker was seen, two
    warehouses tie, or a type only a warehouse with no Apache Ossie dialect reports is
    present; the dialect is then ANSI_SQL and the caller should warn.
    """
    votes = dict.fromkeys(_DIALECT_MARKERS, 0)
    for raw in raw_types:
        base, _ = parse_type(raw)
        if not base:
            continue
        if base in _OTHER_WAREHOUSE_MARKERS:
            return DIALECT_ANSI, False
        for dialect, markers in _DIALECT_MARKERS.items():
            if base in markers:
                votes[dialect] += 1
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    best, best_votes = ranked[0]
    if best_votes == 0:
        return DIALECT_ANSI, False
    if len(ranked) > 1 and ranked[1][1] == best_votes:
        return DIALECT_ANSI, False
    return best, True


def to_ossie_datatype(raw, dialect):
    """Map a raw warehouse type onto an Apache Ossie `datatype`.

    Returns None when the type is absent or unrecognized -- the spec says to omit
    `datatype` when it is unknown rather than guess. A type that is known but outside
    the portable vocabulary (VARIANT, MAP, STRUCT, ...) maps to `Opaque`; the raw name
    is preserved in the SOLID stash either way.
    """
    base, params = parse_type(raw)
    if not base:
        return None
    if base in _DECIMAL_TYPES:
        return INTEGER if _declared_scale(params) == 0 else DECIMAL
    if base.startswith("INTERVAL"):
        # `INTERVAL DAY TO SECOND`, `INTERVAL YEAR TO MONTH`: a qualified interval.
        return OPAQUE
    if base == "TIMESTAMP":
        return _TIMESTAMP_BY_DIALECT.get(dialect, DATE_TIME)
    if base in _TIMESTAMP_VARIANTS:
        return _TIMESTAMP_VARIANTS[base]
    return _COMMON_TYPES.get(base)


def _declared_scale(params):
    """The scale an exact-decimal type declares, or None when it declares none."""
    if len(params) >= 2:
        try:
            return int(params[1])
        except ValueError:
            return None
    return 0 if params else None


def to_raw_type(datatype, dialect):
    """Map an Apache Ossie `datatype` back to a representative warehouse type name.

    Only used when exporting a field that carries no SOLID stash (a hand-authored
    Apache Ossie model). A field that came from Solid re-emits its original raw string.
    """
    if not datatype:
        return None
    table = _DATATYPE_TO_RAW.get(dialect) or _DATATYPE_TO_RAW[DIALECT_ANSI]
    return table.get(datatype)


# Solid splits a table's columns into `facts` and `dimensions` partly by data type, using
# a lowercase prefix match against this set (see semantic_layer_data_types.py in
# solid-server). Mirrored here so a field with nothing else to go on lands in the list
# Solid's type rule would choose (see ossie_to_solid._field_role). Being a prefix match,
# it makes BIGINT and SMALLINT dimensions and INTERVAL a fact, as it does in Solid. Solid
# also weighs column names and cardinality, which an offline converter cannot see.
_SOLID_FACT_TYPE_PREFIXES = (
    "decimal", "double", "int", "float", "number", "numeric", "real", "tiny", "long",
)


def is_solid_fact_type(raw):
    """True if Solid would classify a column of this raw type as a fact."""
    if not isinstance(raw, str):
        return False
    lowered = raw.strip().lower()
    return any(lowered.startswith(p) for p in _SOLID_FACT_TYPE_PREFIXES)


def normalize_dialect(dialect):
    """Validate and upper-case an Apache Ossie dialect name."""
    normalized = str(dialect).strip().upper()
    if normalized not in SUPPORTED_DIALECTS:
        raise ConversionError(
            f"Unsupported dialect '{dialect}'. Choose one of: "
            f"{', '.join(SUPPORTED_DIALECTS)}"
        )
    return normalized


def resolve_dialect(explicit, raw_types, scope="model"):
    """Pick the dialect for a conversion: an explicit choice wins, else inference.

    Warns when inference had to fall back to ANSI_SQL, since a fallback means every
    expression is being labelled with a dialect it was not written in.
    """
    if explicit:
        return normalize_dialect(explicit)
    dialect, confident = infer_dialect(raw_types)
    other = sorted({base for base in (parse_type(raw)[0] for raw in raw_types)
                    if base in _OTHER_WAREHOUSE_MARKERS})
    if other:
        warn(scope, f"the column types ({', '.join(other)}) are from a warehouse with no "
                    f"Apache Ossie dialect of its own, such as Oracle or SQL Server; "
                    f"using {DIALECT_ANSI}. Pass --dialect to set it explicitly.")
    elif not confident:
        warn(
            scope,
            "could not infer the source warehouse from the column type vocabulary; "
            f"defaulting to {DIALECT_ANSI}. Pass --dialect to set it explicitly.",
        )
    return dialect
