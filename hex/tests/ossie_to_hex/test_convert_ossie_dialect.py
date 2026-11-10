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

import pytest
from inline_snapshot import snapshot
from ossie import OssieDialect

from ossie_hex.hex import HexDialect, HexDialectName
from ossie_hex.ossie_to_hex.context import ExportContext
from ossie_hex.ossie_to_hex.convert_ossie_dialect import convert_ossie_dialect
from tests.utils import problems_snapshot


@pytest.fixture
def ctx() -> ExportContext:
    return ExportContext()


@pytest.mark.parametrize(
    ("ossie_dialect", "hex_dialect_name"),
    [
        (OssieDialect.ANSI_SQL, "duckdb"),
        (OssieDialect.OSSIE_SQL_2026, "snowflake"),
        (OssieDialect.BIGQUERY, "bigquery"),
        (OssieDialect.DATABRICKS, "databricks"),
        (OssieDialect.SNOWFLAKE, "snowflake"),
    ],
)
def test_maps_sql_dialect(
    ctx: ExportContext,
    ossie_dialect: OssieDialect,
    hex_dialect_name: HexDialectName,
) -> None:
    result = convert_ossie_dialect(ossie_dialect, ctx=ctx)
    assert result == HexDialect(hex_dialect_name)
    assert not ctx.problems


@pytest.mark.parametrize(
    "ossie_dialect",
    [
        OssieDialect.MAQL,
        OssieDialect.MDX,
        OssieDialect.TABLEAU,
        OssieDialect.SIGMA,
        OssieDialect.THOUGHTSPOT,
        OssieDialect.DAX,
    ],
)
def test_falls_back_to_duckdb_for_expression_language(
    ctx: ExportContext, ossie_dialect: OssieDialect
) -> None:
    result = convert_ossie_dialect(ossie_dialect, ctx=ctx)
    assert result == HexDialect("duckdb")
    assert problems_snapshot(ctx.problems) == snapshot(
        "[INFO] The given Ossie dialect does not map to a Hex dialect; using DuckDB as SQL dialect for Hex semantic project"
    )


def test_falls_back_to_snowflake_for_none(ctx: ExportContext) -> None:
    result = convert_ossie_dialect(None, ctx=ctx)
    assert result == HexDialect("snowflake")
    assert not ctx.problems


def test_handles_unknown_dialect_during_conversion(ctx: ExportContext) -> None:
    ctx.set_ossie_dialect("UNKNOWN_DIALECT")

    with ctx.phase_scope("convert"):
        result = convert_ossie_dialect(ctx.ossie_dialect, ctx=ctx)

    assert result == HexDialect("duckdb")
    assert ctx.ossie_dialect == "UNKNOWN_DIALECT"
    assert problems_snapshot(ctx.problems) == snapshot(
        "[INFO] The given Ossie dialect does not map to a Hex dialect; using DuckDB as SQL dialect for Hex semantic project"
    )


def test_well_known_dialect_mixed_case(ctx: ExportContext) -> None:
    """A well-known dialect specified in any casing should be normalized
    and mapped to the corresponding Hex dialect."""
    ossie_dialect = "BigQuery"

    result = convert_ossie_dialect(ossie_dialect, ctx=ctx)

    assert result == HexDialect("bigquery")
    assert not ctx.problems


def test_unknown_dialect(
    ctx: ExportContext,
) -> None:
    """An unknown dialect should be preserved as-is with a problem.

    This is also the "typo" case.
    """
    ossie_dialect = "snowflak"

    result = convert_ossie_dialect(ossie_dialect, ctx=ctx)

    assert result == HexDialect("duckdb")
    assert problems_snapshot(ctx.problems) == snapshot(
        "[INFO] The given Ossie dialect does not map to a Hex dialect; using DuckDB as SQL dialect for Hex semantic project"
    )
