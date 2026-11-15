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

"""Preserves the joined dataset grain for sums of declared integer columns."""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import NamedTuple

from ossie_databricks.ossie_converter_common import (
    find_outside_literals,
    replace_outside_literals,
)

_ID = "[A-Za-z_][A-Za-z0-9_]*"
_COLUMN = _ID + r"(?:\." + _ID + ")+"
_QUALIFIED_COLUMN = re.compile(_COLUMN)
_SUM = re.compile(r"(?<![\w.])SUM\s*\(\s*(" + _COLUMN + r")\s*\)", re.IGNORECASE | re.ASCII)
_AGGREGATE_MODIFIER = re.compile(
    r"\s*(?:(?:/\*.*?\*/|--[^\r\n]*)\s*)*(?:FILTER|OVER)\b",
    re.IGNORECASE | re.DOTALL | re.ASCII,
)
_PREFIX = "AGGREGATE(COLLECT_SET(STRUCT("
_SEPARATOR = " AS ossie_key, "
_FILTER = " AS ossie_value)) FILTER (WHERE "
_NOT_NULL = " IS NOT NULL"
_SUFFIX = (
    "), CAST(NULL AS BIGINT), "
    "(ossie_acc, ossie_row) -> COALESCE(ossie_acc, 0L) + CAST(ossie_row.ossie_value AS BIGINT))"
)
_ENCODED = re.compile(
    r"\bAGGREGATE\s*\(\s*COLLECT_SET\s*\(\s*STRUCT\s*\(\s*("
    + _COLUMN
    + r")\s+AS\s+ossie_key\s*,\s*("
    + _COLUMN
    + r")\s+AS\s+ossie_value\s*\)\s*\)\s*FILTER\s*\(\s*WHERE\s+"
    r"\1\s+IS\s+NOT\s+NULL\s+AND\s+\2\s+IS\s+NOT\s+NULL\s*\)\s*,\s*"
    r"CAST\s*\(\s*NULL\s+AS\s+BIGINT\s*\)\s*,\s*"
    r"\(\s*ossie_acc\s*,\s*ossie_row\s*\)\s*->\s*"
    r"COALESCE\s*\(\s*ossie_acc\s*,\s*0L\s*\)\s*\+\s*"
    r"CAST\s*\(\s*ossie_row\.ossie_value\s+AS\s+BIGINT\s*\)\s*\)",
    re.IGNORECASE | re.ASCII,
)


class Decoded(NamedTuple):
    expression: str
    physical_columns: frozenset[str]
    keys: dict[str, str]


def encode(expression: str, keys: Mapping[str, str], grain_paths: AbstractSet[str]) -> str | None:
    if not grain_paths:
        return expression
    # A quoted identifier may name a join path that the unquoted matcher cannot prove.
    if "`" in expression or '"' in expression:
        return None
    remaining = list(expression)
    rewritten_paths: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        value = match.group(1)
        key = keys.get(value)
        path = qualifier(value)
        if (
            key is None
            or _QUALIFIED_COLUMN.fullmatch(key) is None
            or qualifier(key) != path
            or path not in grain_paths
            or _AGGREGATE_MODIFIER.match(expression, match.end()) is not None
        ):
            return match.group()
        # The struct deduplicates rows by key, including stores with identical values. Removing
        # null operands and starting with NULL preserve SUM on unmatched and all-null groups.
        for i in range(match.start(), match.end()):
            remaining[i] = " "
        rewritten_paths.add(path)
        return (
            _PREFIX + key + _SEPARATOR + value + _FILTER + key + _NOT_NULL
            + " AND " + value + _NOT_NULL + _SUFFIX
        )  # fmt: skip

    encoded = replace_outside_literals(expression, _SUM, replace)
    # If any operand on a hinted path remains, rewriting only the supported sums could combine
    # different grains in one measure. Keep the entire measure as-is.
    path_pattern = "|".join(
        r"\s*\.\s*".join(re.escape(part) for part in _java_split_dot(path)) for path in grain_paths
    )
    reference = re.compile(
        r"(?<![\w.])(?:" + path_pattern + r")\s*\.", re.IGNORECASE | re.ASCII
    )
    if rewritten_paths >= set(grain_paths) and not find_outside_literals(
        "".join(remaining), reference
    ):
        return encoded
    return None


def _java_split_dot(path: str) -> list[str]:
    parts = path.split(".")
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return parts


def qualifier(column: str) -> str:
    return column[: column.rfind(".")]


def decode(expression: str) -> Decoded:
    columns: set[str] = set()
    keys: dict[str, str] = {}
    path_keys: dict[str, str] = {}
    conflicting_paths: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        value = match.group(2)
        key = match.group(1)
        if qualifier(value).lower() != qualifier(key).lower():
            return match.group()
        path = qualifier(value)
        prior_key = path_keys.setdefault(path, key)
        if prior_key is not key and prior_key.lower() != key.lower():
            conflicting_paths.add(path)
        keys[value] = key
        columns.add(value[value.rfind(".") + 1 :].lower())
        return "SUM(" + value + ")"

    decoded = replace_outside_literals(expression, _ENCODED, replace)
    if conflicting_paths or encode(decoded, keys, path_keys.keys()) is None:
        return Decoded(expression, frozenset(), {})
    return Decoded(decoded, frozenset(columns), keys)
