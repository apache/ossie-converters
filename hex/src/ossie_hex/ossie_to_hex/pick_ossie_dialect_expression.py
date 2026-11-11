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

from ossie import OssieDialectExpression

from ..ossie import (
    LEGACY_PORTABLE_OSSIE_DIALECT,
    OSSIE_SQL_DIALECTS,
    PORTABLE_OSSIE_DIALECT,
)
from .context import ExportContext


def pick_ossie_dialect_expression(
    ossie_dialect_expressions: list[OssieDialectExpression] | None,
    *,
    ctx: ExportContext,
) -> OssieDialectExpression | None:
    """Pick an Ossie dialect expression from a list of Ossie dialect expressions.

    Prefers to pick the current dialect, then falls back to a portable dialect,
    then the first in the list.

    Returns a dialect expression, or None if there are no dialect expressions.
    """
    if not ossie_dialect_expressions:
        # An expression may carry only the portable `sql`, which is not read yet.
        if ctx.current_phase_name == "load":
            ctx.error("No dialect expression found; `sql` is not supported")
        return None
    fallback = ossie_dialect_expressions[0]
    for entry in ossie_dialect_expressions:
        if (
            ctx.ossie_dialect is not None
            # case-insensitive for well-known dialects
            and entry.dialect.upper() == ctx.ossie_dialect.upper()
        ):
            return entry
        # fallback preference: (1) portable dialect, (2) first in the list
        elif entry.dialect == PORTABLE_OSSIE_DIALECT or (
            entry.dialect == LEGACY_PORTABLE_OSSIE_DIALECT
            and fallback.dialect != PORTABLE_OSSIE_DIALECT
        ):
            fallback = entry
    if ctx.current_phase_name == "load":
        if ctx.ossie_dialect is not None and (
            fallback.dialect
            not in (PORTABLE_OSSIE_DIALECT, LEGACY_PORTABLE_OSSIE_DIALECT)
            # the portable dialects map to different Hex dialects
            or (
                fallback.dialect == PORTABLE_OSSIE_DIALECT
                and ctx.ossie_dialect.upper() == LEGACY_PORTABLE_OSSIE_DIALECT
            )
        ):
            # explicit preference is missing and fallback is incompatible
            ctx.warn(
                f"Preferred dialect {ctx.ossie_dialect} not found for expression; using fallback dialect {fallback.dialect}"
            )
        if fallback.dialect not in OSSIE_SQL_DIALECTS:
            ctx.warn(
                f"Chosen dialect {fallback.dialect} is not a recognized SQL dialect and may not be supported by Hex"
            )
    return fallback
