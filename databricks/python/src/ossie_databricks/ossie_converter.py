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

"""Bidirectional converter between Apache Ossie semantic models and Databricks Metric Views.

This module is the public facade, re-exporting the two conversion entry points and the shared
:class:`ConversionError` / :class:`Notices` / :class:`Result` types. The implementation is split by
direction so each half can be read on its own:

* :mod:`ossie_databricks.ossie_converter_common` -- shared constants, YAML I/O, and map accessors
* :mod:`ossie_databricks.ossie_to_metric_view` -- EXPORT: Apache Ossie -> Metric View
* :mod:`ossie_databricks.metric_view_to_ossie` -- IMPORT: Metric View -> Apache Ossie

The authoritative contract is Metric View YAML v1.1 as defined by the Databricks serde and its
validation rules; the checked-in YAML fixtures pin the expected output for both directions.

Conversion operates on parsed YAML as plain dicts and lists rather than typed models, so the
converter stays independent of any engine. Warnings ("drops") are collected into a
:class:`Notices` buffer and returned rather than written to stderr, so a caller can present them.

This is the Python twin of the Java ``org.apache.ossie.converter.databricks.OssieConverter``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

OSSIE_VERSION = "0.2.0.dev0"
MV_VERSION = "1.1"


class ConversionErrorKind(enum.Enum):
    """Stable failure categories for callers that need structured error handling."""

    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    NOT_REPRESENTABLE = "NOT_REPRESENTABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ConversionError(Exception):
    """Raised for any input the converter refuses to convert."""

    def __init__(
        self,
        message: str,
        kind: ConversionErrorKind = ConversionErrorKind.NOT_REPRESENTABLE,
        reason: str | None = None,
        unsupported_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.reason = message if reason is None else reason
        self.unsupported_version = unsupported_version

    @classmethod
    def invalid_input(cls, message: str, reason: str) -> ConversionError:
        return cls(message, ConversionErrorKind.INVALID_INPUT, reason)

    @classmethod
    def for_unsupported_version(cls, message: str, version: str) -> ConversionError:
        return cls(message, ConversionErrorKind.UNSUPPORTED_VERSION, message, version)

    @classmethod
    def internal_error(cls, message: str) -> ConversionError:
        return cls(message, ConversionErrorKind.INTERNAL_ERROR, message)

    def __str__(self) -> str:
        return self.message


class Notices:
    """Collects drop/rewrite notices during a conversion."""

    def __init__(self) -> None:
        self._messages: list[str] = []

    def warn(self, scope: str, msg: str) -> None:
        self._messages.append(f"[{scope}] {msg}")

    def to_list(self) -> list[str]:
        return list(self._messages)


@dataclass
class Result:
    """Result of a conversion: the emitted YAML plus any drop notices."""

    yaml: str
    notices: list[str] = field(default_factory=list)


def convert_ossie_to_metric_view(ossie_yaml: str, source: str | None = None) -> Result:
    """EXPORT: Apache Ossie semantic model YAML -> Metric View v1.1 YAML."""
    from ossie_databricks.ossie_to_metric_view import convert_ossie_to_metric_view as convert

    return convert(ossie_yaml, source)


def convert_metric_view_to_ossie(mv_yaml: str, model_name: str | None = None) -> Result:
    """IMPORT: Metric View v1.1 YAML -> Apache Ossie semantic model YAML."""
    from ossie_databricks.metric_view_to_ossie import convert_metric_view_to_ossie as convert

    return convert(mv_yaml, model_name)


def parse_yaml(s: str) -> object:
    """Parse YAML text into a plain value (YAML 1.2 booleans, matching the converter)."""
    from ossie_databricks.ossie_converter_common import parse_yaml as parse

    return parse(s)


def dump_yaml(obj: object) -> str:
    """Serialize a value to YAML using the converter's writer."""
    from ossie_databricks.ossie_converter_common import dump_yaml as dump

    return dump(obj)
