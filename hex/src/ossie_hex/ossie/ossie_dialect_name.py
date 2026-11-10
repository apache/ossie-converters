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

from typing import Annotated

from ossie import OssieDialect

OssieDialectName = Annotated[
    str,
    """An Ossie dialect identifier for Ossie expression entries.

    Can be a well-known OssieDialect value or a free-form string. The value
    may represent a SQL engine or expression language.
    
    Well-known values are uppercase ASCII identifiers in `SCREAMING_SNAKE_CASE`.
    Any string may appear when carrying a dialect preference; it is not
    required to match a fixed catalog of names. Values are kept as written
    and compared case-insensitively.
    """,
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
