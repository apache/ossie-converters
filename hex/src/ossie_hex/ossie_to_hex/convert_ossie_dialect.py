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

from collections.abc import Mapping

from ossie import OssieDialect

from ossie_hex.ossie import OssieDialectName

from ..hex import HexDialect, HexDialectName
from .context import ExportContext

_DIALECT_NAME_MAP: Mapping[OssieDialectName, HexDialectName] = {
    # enumerated dialects that are not a well-known dialect
    OssieDialect.ANSI_SQL.value: "duckdb",
    OssieDialect.OSSIE_SQL_2026.value: "snowflake",  # Match the approximation used for parsing
    # raw strings -- well known dialects
    "ALLOYDB": "alloydb",
    "ATHENA": "athena",
    "BIGQUERY": "bigquery",
    "CLICKHOUSE": "clickhouse",
    "DATABRICKS": "databricks",
    "DUCKDB": "duckdb",
    "MOTHERDUCK": "motherduck",
    "MSSQL": "mssql",
    "MYSQL": "mysql",
    "POSTGRES": "postgres",
    "PRESTODB": "prestodb",
    "REDSHIFT": "redshift",
    "SNOWFLAKE": "snowflake",
    "SPARK": "spark",
    "STARBURST": "starburst",
    "TRINO": "trino",
    "TSQL": "tsql",
}
"""
Map of well-known dialect names that could appear in Ossie documents to dialect
names supported by Hex.
"""


def convert_ossie_dialect(
    ossie_dialect: OssieDialectName | None,
    *,
    ctx: ExportContext,
) -> HexDialect:
    if ossie_dialect is None:
        return HexDialect("duckdb")
    if ossie_dialect not in _DIALECT_NAME_MAP:
        ctx.info(
            "The given Ossie dialect does not map to a Hex dialect; using DuckDB as SQL dialect for Hex semantic project"
        )
        return HexDialect("duckdb")
    return HexDialect(_DIALECT_NAME_MAP[ossie_dialect])
