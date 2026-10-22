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
"""Column types from a dbt ``catalog.json``.

``dbt docs generate`` records the warehouse's real type for every column of
every model. Lightdash learns most dimension types from the warehouse rather
than from YAML, so a Lightdash project usually has few authored types; the
catalog fills the gaps with the physical type reduced to Ossie's logical
vocabulary.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from ossie import OssieDataType

from ossie_lightdash.converter_issues import ConversionError

# Column types keyed by model name, then by lower-cased column name.
Catalog = Dict[str, Dict[str, str]]

_TYPE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9 ]*?)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\))?\s*$")

_EXACT = {
    "INT": OssieDataType.INTEGER,
    "INTEGER": OssieDataType.INTEGER,
    "INT64": OssieDataType.INTEGER,
    "BIGINT": OssieDataType.INTEGER,
    "SMALLINT": OssieDataType.INTEGER,
    "TINYINT": OssieDataType.INTEGER,
    "INT2": OssieDataType.INTEGER,
    "INT4": OssieDataType.INTEGER,
    "INT8": OssieDataType.INTEGER,
    "FLOAT": OssieDataType.FLOAT,
    "FLOAT4": OssieDataType.FLOAT,
    "FLOAT8": OssieDataType.FLOAT,
    "FLOAT64": OssieDataType.FLOAT,
    "DOUBLE": OssieDataType.FLOAT,
    "DOUBLE PRECISION": OssieDataType.FLOAT,
    "REAL": OssieDataType.FLOAT,
    "STRING": OssieDataType.STRING,
    "VARCHAR": OssieDataType.STRING,
    "NVARCHAR": OssieDataType.STRING,
    "CHAR": OssieDataType.STRING,
    "CHARACTER": OssieDataType.STRING,
    "CHARACTER VARYING": OssieDataType.STRING,
    "TEXT": OssieDataType.STRING,
    "BOOL": OssieDataType.BOOLEAN,
    "BOOLEAN": OssieDataType.BOOLEAN,
    "DATE": OssieDataType.DATE,
    "DATETIME": OssieDataType.DATE_TIME,
    "TIMESTAMP": OssieDataType.DATE_TIME,
    "TIMESTAMP_NTZ": OssieDataType.DATE_TIME,
    "TIMESTAMP WITHOUT TIME ZONE": OssieDataType.DATE_TIME,
    "TIMESTAMPTZ": OssieDataType.DATE_TIME_TZ,
    "TIMESTAMP_TZ": OssieDataType.DATE_TIME_TZ,
    "TIMESTAMP_LTZ": OssieDataType.DATE_TIME_TZ,
    "TIMESTAMP WITH TIME ZONE": OssieDataType.DATE_TIME_TZ,
    "TIME": OssieDataType.TIME,
}
# Exact-decimal families: integral when the scale is 0 (or, for NUMBER, when
# Snowflake's default NUMBER(38,0) is spelled without arguments).
_DECIMAL_FAMILY = {"NUMERIC", "BIGNUMERIC", "DECIMAL", "NUMBER"}


def warehouse_type_to_datatype(warehouse_type: str) -> Optional[OssieDataType]:
    """Reduce a physical column type to an Ossie datatype, or None when the
    type is outside the portable vocabulary (arrays, structs, JSON, ...)."""
    match = _TYPE_RE.match(warehouse_type or "")
    if match is None:
        return None
    base = " ".join(match.group(1).upper().split())
    scale = match.group(3)
    if base in _DECIMAL_FAMILY:
        if scale is not None:
            return OssieDataType.INTEGER if int(scale) == 0 else OssieDataType.DECIMAL
        return OssieDataType.INTEGER if base == "NUMBER" else OssieDataType.DECIMAL
    return _EXACT.get(base)


def load_catalog(path: Path) -> Catalog:
    """Read a dbt ``catalog.json`` into ``{model: {column: type}}``.

    Models and seeds are keyed by the node's ``name``, which is the name
    Lightdash uses for the model. The last segment of a node's unique ID is
    not that name: a versioned model's unique ID ends in ``.v2``. When a
    model has several versions, the latest version (``latest_version``, or
    the highest ``version``) supplies the types. Two packages that each
    define a model with the same name make the catalog ambiguous for that
    name, which raises a ``ConversionError`` rather than letting one package
    silently supply the other's types. Column names are lower-cased because
    warehouses differ in how they report case.

    Raises:
        ConversionError: If the file is not a JSON object with a ``nodes``
            mapping, or a model name is defined by more than one package.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConversionError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ConversionError(f"{path} is not a dbt catalog: expected a JSON object")
    nodes = document.get("nodes")
    if nodes is None:
        nodes = {}
    if not isinstance(nodes, dict):
        raise ConversionError(f"{path} is not a dbt catalog: `nodes` must be an object")

    catalog: Catalog = {}
    # (package, version) of the node that supplied each name's types.
    provenance: Dict[str, Any] = {}
    for unique_id, node in nodes.items():
        if not isinstance(node, dict):
            raise ConversionError(f"{path}: node {unique_id} must be an object")
        kind = unique_id.split(".", 1)[0]
        if kind not in ("model", "seed"):
            continue
        name = node.get("name") or _name_from_unique_id(unique_id, node)
        package = node.get("package_name") or unique_id.split(".")[1:2]
        version = node.get("version")
        previous = provenance.get(name)
        if previous is not None:
            previous_package, previous_version = previous
            if previous_package != package:
                raise ConversionError(
                    f"{path} names model {name!r} in two packages "
                    f"({previous_package} and {package}); the catalog cannot say "
                    f"which one a Lightdash model called {name!r} is built from"
                )
            # Several versions of one model: the latest one supplies the types.
            if not _is_newer_version(node, version, previous_version):
                continue
        provenance[name] = (package, version)
        columns = node.get("columns")
        if columns is None:
            columns = {}
        if not isinstance(columns, dict):
            raise ConversionError(f"{path}: columns of {unique_id} must be an object")
        catalog[name] = {
            column_name.lower(): ((column or {}).get("type") or "")
            for column_name, column in columns.items()
        }
    return catalog


def _name_from_unique_id(unique_id: str, node: Dict[str, Any]) -> str:
    """The model name for a node that carries no ``name``: the last segment
    of the unique ID, or the one before it for a versioned model
    (``model.pkg.orders.v2``)."""
    segments = unique_id.split(".")
    if node.get("version") is not None and len(segments) >= 4 and re.fullmatch(r"v\w+", segments[-1]):
        return segments[-2]
    return segments[-1]


def _is_newer_version(node: Dict[str, Any], version: Any, previous_version: Any) -> bool:
    """True when ``node`` should replace the version already recorded."""
    latest = node.get("latest_version")
    if latest is not None:
        return str(version) == str(latest)
    try:
        return float(version) > float(previous_version)
    except (TypeError, ValueError):
        return str(version) > str(previous_version)
