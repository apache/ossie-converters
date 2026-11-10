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

from typing import Literal

from ossie import OssieDialect

OssieDialectName = Literal[
    "ANSI_SQL",
    "SNOWFLAKE",
    "MDX",
    "MAQL",
    "TABLEAU",
    "DATABRICKS",
    "BIGQUERY",
    "THOUGHTSPOT",
    "SIGMA",
    "DAX",
    "OSSIE_SQL_2026",
]


PORTABLE_OSSIE_DIALECT = OssieDialect.OSSIE_SQL_2026
"""The canonical portable dialect of Ossie."""

LEGACY_PORTABLE_OSSIE_DIALECT = OssieDialect.ANSI_SQL
"""The legacy portable dialect of Ossie.

A significant subset of converters still output ANSI_SQL as if it were the
portable, default dialect of Ossie.
"""

OSSIE_SQL_DIALECTS = [
    OssieDialect.OSSIE_SQL_2026,
    OssieDialect.ANSI_SQL,
    OssieDialect.BIGQUERY,
    OssieDialect.DATABRICKS,
    OssieDialect.SNOWFLAKE,
]
