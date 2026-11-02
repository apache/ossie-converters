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

from ossie import OssieDialect, OssieDialectExpression

from .context import ExportContext


def pick_ossie_dialect_expression(
    ossie_dialect_expressions: list[OssieDialectExpression],
    *,
    ctx: ExportContext,
) -> OssieDialectExpression | None:
    """Pick an Ossie dialect expression from a list of Ossie dialect expressions.

    Prefers to pick the current dialect, then falls back to a portable dialect,
    then the first in the list.

    Returns a dialect expression or None if no fallback is found.
    """
    fallback: OssieDialectExpression | None = None
    for entry in ossie_dialect_expressions:
        if ctx.ossie_dialect is not None and entry.dialect == ctx.ossie_dialect:
            return entry
        # fallback preference: (1) portable dialect, (2) first in the list
        elif (
            fallback is None
            or entry.dialect == PORTABLE_OSSIE_DIALECT
            or (
                entry.dialect == LEGACY_PORTABLE_OSSIE_DIALECT
                and fallback.dialect != PORTABLE_OSSIE_DIALECT
            )
        ):
            fallback = entry
    if ctx.current_phase_name == "load":
        if fallback is None:
            ctx.error("No dialect found for expression")
        elif ctx.ossie_dialect is not None and fallback.dialect not in (
            PORTABLE_OSSIE_DIALECT,
            LEGACY_PORTABLE_OSSIE_DIALECT,
        ):
            # explicit preference is missing and fallback is non-portable
            ctx.warn(
                f"Preferred dialect {ctx.ossie_dialect} not found for expression; using fallback dialect {fallback.dialect}"
            )
    return fallback


PORTABLE_OSSIE_DIALECT = OssieDialect.OSSIE_SQL_2026
"""The canonical portable dialect of Ossie."""

LEGACY_PORTABLE_OSSIE_DIALECT = OssieDialect.ANSI_SQL
"""The legacy portable dialect of Ossie.

A significant subset of converters still output ANSI_SQL as if it were the
portable, default dialect of Ossie.
"""
