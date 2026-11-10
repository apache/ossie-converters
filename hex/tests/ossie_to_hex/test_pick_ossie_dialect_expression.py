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

from collections.abc import Iterator

import pytest
from inline_snapshot import snapshot
from ossie import OssieDialect, OssieDialectExpression, OssieExpression

from ossie_hex.ossie_to_hex.context import ExportContext
from ossie_hex.ossie_to_hex.pick_ossie_dialect_expression import (
    pick_ossie_dialect_expression,
)
from tests.ossie_to_hex.utils import Quick
from tests.utils import problems_snapshot


@pytest.fixture
def ctx() -> Iterator[ExportContext]:
    ctx = ExportContext()
    ctx.set_ossie_dialect(None)
    with ctx.phase_scope("load"):
        yield ctx


@pytest.mark.parametrize("preferred", ["ANSI_SQL", "SNOWFLAKE"])
def test_picks_preferred_variant(ctx: ExportContext, preferred: str) -> None:
    preferred_dialect = Quick.dialect(preferred)
    ctx.set_ossie_dialect(preferred_dialect)
    expression = Quick.expression(
        [
            ("DAX", "SELECT FROM"),
            ("ANSI_SQL", "foo.bar"),
            ("SNOWFLAKE", "foo.bar + 1"),
            ("OSSIE_SQL_2026", "foo.bar + 2"),
        ]
    )

    assert expression.dialects is not None
    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    selected = next(
        entry for entry in expression.dialects if entry.dialect == preferred
    )
    assert result is selected
    assert problems_snapshot(ctx.problems) == snapshot("")


def test_picks_preferred_variant_case_insensitively(ctx: ExportContext) -> None:
    ossie_dialect = "snowflake"
    ctx.set_ossie_dialect(ossie_dialect)
    expression = Quick.expression(
        [
            ("ANSI_SQL", "foo.bar"),
            ("SNOWFLAKE", "foo.bar + 1"),
        ]
    )

    assert expression.dialects is not None
    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is expression.dialects[1]
    assert not ctx.problems


def test_prefers_legacy_portable_dialect_over_other_variants(
    ctx: ExportContext,
) -> None:
    preferred_dialect = Quick.dialect("SNOWFLAKE")
    ctx.set_ossie_dialect(preferred_dialect)
    expression = Quick.expression(
        [
            ("BIGQUERY", "foo.bar + 1"),
            ("ANSI_SQL", "foo.bar"),
        ]
    )

    assert expression.dialects is not None
    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is expression.dialects[-1]
    assert not ctx.problems


def test_prefers_canonical_portable_dialect_over_other_variants(
    ctx: ExportContext,
) -> None:
    preferred_dialect = Quick.dialect("SNOWFLAKE")
    ctx.set_ossie_dialect(preferred_dialect)
    expression = Quick.expression(
        [
            ("BIGQUERY", "foo.bar + 1"),
            ("ANSI_SQL", "foo.bar"),
            ("OSSIE_SQL_2026", "foo.bar + 2"),
        ]
    )

    assert expression.dialects is not None
    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is expression.dialects[-1]
    assert not ctx.problems


def test_warns_when_legacy_portable_dialect_falls_back_to_canonical(
    ctx: ExportContext,
) -> None:
    preferred_ossie_dialect = Quick.dialect("ANSI_SQL")
    ctx.set_ossie_dialect(preferred_ossie_dialect)
    expression = Quick.expression([("OSSIE_SQL_2026", "DATEADD(day, 1, foo.bar)")])

    assert expression.dialects is not None
    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is expression.dialects[0]
    assert problems_snapshot(ctx.problems) == snapshot(
        "[WARNING] Preferred dialect OssieDialect.ANSI_SQL not found for expression; using fallback dialect OSSIE_SQL_2026"
    )


def test_falls_back_to_first_variant_when_preferred_and_ansi_are_missing(
    ctx: ExportContext,
) -> None:
    expression = Quick.expression([("BIGQUERY", "foo.bar"), ("DAX", "SELECT FROM")])

    assert expression.dialects is not None
    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is expression.dialects[0]
    assert problems_snapshot(ctx.problems) == snapshot("")


def test_warns_when_preferred_and_portable_dialects_are_missing(
    ctx: ExportContext,
) -> None:
    ctx.set_ossie_dialect(OssieDialect.SNOWFLAKE)
    expression = Quick.expression(
        [
            ("BIGQUERY", "foo.bar"),
            ("DAX", "SELECT FROM"),
        ]
    )

    assert expression.dialects is not None
    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is expression.dialects[0]
    assert problems_snapshot(ctx.problems) == snapshot(
        "[WARNING] Preferred dialect OssieDialect.SNOWFLAKE not found for expression; using fallback dialect BIGQUERY"
    )


def test_picks_sql_when_there_are_no_dialect_expressions(
    ctx: ExportContext,
) -> None:
    expression = OssieExpression(sql="foo.bar")

    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is not None
    assert (result.dialect, result.expression) == ("OSSIE_SQL_2026", "foo.bar")
    assert problems_snapshot(ctx.problems) == snapshot("")


@pytest.mark.parametrize("portable", ["OSSIE_SQL_2026", "ossie_sql_2026"])
def test_sql_wins_over_a_canonical_portable_dialect_expression(
    ctx: ExportContext, portable: str
) -> None:
    expression = OssieExpression(
        sql="foo.bar",
        dialects=[
            OssieDialectExpression(dialect="DAX", expression="SELECT FROM"),
            OssieDialectExpression(dialect=portable, expression="foo.stale"),
        ],
    )

    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert result is not None
    assert result.expression == "foo.bar"


def test_preferred_variant_wins_over_sql(ctx: ExportContext) -> None:
    ctx.set_ossie_dialect(OssieDialect.SNOWFLAKE)
    expression = Quick.expression([("SNOWFLAKE", "foo.native")])
    expression = expression.model_copy(update={"sql": "foo.bar"})

    result = pick_ossie_dialect_expression(expression, ctx=ctx)

    assert expression.dialects is not None
    assert result is expression.dialects[0]
