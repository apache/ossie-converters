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

"""Results, warnings, and the error type shared by the verbs."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class ConversionError(Exception):
    """The input cannot be converted."""


class WarningCode(StrEnum):
    PORTABLE_EXPRESSION_MISSING = "PORTABLE_EXPRESSION_MISSING"
    METRIC_NOT_PORTABLE = "METRIC_NOT_PORTABLE"
    NOT_DEPLOYED = "NOT_DEPLOYED"
    NOT_TRANSLATED = "NOT_TRANSLATED"
    NO_SQL_EXPRESSION = "NO_SQL_EXPRESSION"
    SYNONYMS_TRUNCATED = "SYNONYMS_TRUNCATED"
    METRIC_VIEW_HAS_PARAMETERS = "METRIC_VIEW_HAS_PARAMETERS"


@dataclass(frozen=True)
class ConversionWarning:
    code: WarningCode
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] at {self.path}: {self.message}"


@dataclass
class Result(Generic[T]):
    value: T
    warnings: list[ConversionWarning] = field(default_factory=list)

    def warn(self, code: WarningCode, path: str, message: str) -> None:
        self.warnings.append(ConversionWarning(code, path, message))
