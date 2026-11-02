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

"""Translation between Ossie datatypes and Lightdash dimension types.

Ossie has a portable logical datatype on every field. Lightdash has a
coarser set of dimension types, so the mapping loses information in one
direction: several Ossie numeric and temporal types map to a single Lightdash
type. A round trip preserves the category of a datatype but not always its
exact member. For example, ``Integer`` returns as ``Decimal``.
"""

from typing import Optional

from ossie import OssieDataType

# Ossie datatype -> Lightdash dimension type.
_DATATYPE_TO_LIGHTDASH = {
    OssieDataType.STRING: "string",
    OssieDataType.INTEGER: "number",
    OssieDataType.DECIMAL: "number",
    OssieDataType.FLOAT: "number",
    OssieDataType.BOOLEAN: "boolean",
    OssieDataType.DATE: "date",
    OssieDataType.DATE_TIME: "timestamp",
    OssieDataType.DATE_TIME_TZ: "timestamp",
    # Lightdash has no time-of-day dimension type. A string keeps the value
    # visible instead of dropping the column.
    OssieDataType.TIME: "string",
    OssieDataType.OPAQUE: "string",
}

# Lightdash dimension type -> Ossie datatype. Lightdash doesn't express numeric
# widths, so `number` maps to the widest exact type.
_LIGHTDASH_TO_DATATYPE = {
    "string": OssieDataType.STRING,
    "number": OssieDataType.DECIMAL,
    "boolean": OssieDataType.BOOLEAN,
    "date": OssieDataType.DATE,
    "timestamp": OssieDataType.DATE_TIME,
}

_TEMPORAL = {OssieDataType.DATE, OssieDataType.TIME, OssieDataType.DATE_TIME, OssieDataType.DATE_TIME_TZ}


def datatype_to_lightdash_type(datatype: Optional[OssieDataType]) -> Optional[str]:
    """Gets the Lightdash dimension type for an Ossie datatype, or None."""
    if datatype is None:
        return None
    return _DATATYPE_TO_LIGHTDASH.get(datatype)


def lightdash_type_to_datatype(lightdash_type: Optional[str]) -> Optional[OssieDataType]:
    """Gets the Ossie datatype for a Lightdash dimension type, or None."""
    if lightdash_type is None:
        return None
    return _LIGHTDASH_TO_DATATYPE.get(lightdash_type)


_COUNT_TYPES = {"count", "count_distinct"}
_NUMERIC_AGGREGATES = {
    "sum", "sum_distinct", "average", "average_distinct", "median", "percentile"
}
_ORDER_AGGREGATES = {"min", "max"}
_VALUE_TYPES = {
    "boolean": OssieDataType.BOOLEAN,
    "string": OssieDataType.STRING,
    "date": OssieDataType.DATE,
    "timestamp": OssieDataType.DATE_TIME,
}


def metric_datatype(
    lightdash_type: str, column_type: Optional[str]
) -> Optional[OssieDataType]:
    """Derives the Ossie datatype of a Lightdash metric's result.

    Counts are integers. Numeric aggregates over a `number` column are
    decimals. `min` and `max` keep the column's type. Value-typed metrics
    (`boolean`, `string`, `date`, `timestamp`) declare their own type.

    Returns:
        The datatype, or None when the type isn't implied (`number` with
        arbitrary SQL, or an aggregate over an untyped column).
    """
    if lightdash_type in _COUNT_TYPES:
        return OssieDataType.INTEGER
    if lightdash_type in _VALUE_TYPES:
        return _VALUE_TYPES[lightdash_type]
    if lightdash_type in _NUMERIC_AGGREGATES and column_type == "number":
        return OssieDataType.DECIMAL
    if lightdash_type in _ORDER_AGGREGATES:
        return lightdash_type_to_datatype(column_type)
    return None


def is_temporal(datatype: Optional[OssieDataType]) -> bool:
    """Checks whether the datatype represents a point in time."""
    return datatype in _TEMPORAL
