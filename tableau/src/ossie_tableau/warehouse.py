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

"""Warehouse descriptors: everything the converter needs to know about a target SQL warehouse.

Add a warehouse by adding a `Warehouse` entry to `WAREHOUSES`. `DEFAULT` is used wherever no warehouse is chosen.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field

ANSI_SQL = "ANSI_SQL"


@dataclass(frozen=True)
class Warehouse:
    name: str
    tableau_class: str
    sqlglot_dialect: str
    ossie_dialect: str
    extra_attributes: tuple[str, ...] = ()
    omit_attributes: tuple[str, ...] = ()  # default connection attributes this warehouse does not use
    fixed_attributes: tuple[tuple[str, str], ...] = ()  # constant connection attributes (override the defaults)
    database_attribute: str = "dbname"  # connection attribute that carries Connection.database
    quote: str = '"'  # identifier quote character
    default_server: str = ""  # used when no server is given (BigQuery has a single endpoint)

    @property
    def dialect_preference(self) -> tuple[str, str]:
        return (self.ossie_dialect, ANSI_SQL)


# Fallback for connection classes that are not registered: generic sqlglot dialect, ANSI_SQL expressions. Not in WAREHOUSES (not selectable).
GENERIC = Warehouse(name="generic", tableau_class="", sqlglot_dialect="", ossie_dialect="ANSI_SQL")

SNOWFLAKE = Warehouse(
    name="snowflake",
    tableau_class="snowflake",
    sqlglot_dialect="snowflake",
    ossie_dialect="SNOWFLAKE",
    extra_attributes=("warehouse",),
)

# Attribute set read from a Tableau-saved BigQuery connection (corpus: reviews.twb). `authentication="oauth"` is not confirmed
# against a fresh Tableau export; see LIMITATIONS.md.
BIGQUERY = Warehouse(
    name="bigquery",
    tableau_class="bigquery",
    sqlglot_dialect="bigquery",
    ossie_dialect="BIGQUERY",
    extra_attributes=("project", "EXECCATALOG"),
    omit_attributes=("dbname", "odbc-connect-string-extras", "one-time-sql", "service"),
    fixed_attributes=(("authentication", "oauth"), ("connection-dialect", "google-sql")),
    database_attribute="CATALOG",
    quote="`",
    default_server="googleapis.com/bigquery",
)

WAREHOUSES: dict[str, Warehouse] = {w.name: w for w in (SNOWFLAKE, BIGQUERY)}
DEFAULT = SNOWFLAKE


def get(name: str) -> Warehouse:
    try:
        return WAREHOUSES[name]
    except KeyError:
        raise ValueError(f"unknown warehouse {name!r}; known: {', '.join(sorted(WAREHOUSES))}") from None


def by_tableau_class(tableau_class: str) -> Warehouse | None:
    return next((w for w in WAREHOUSES.values() if w.tableau_class == tableau_class), None)


@dataclass(frozen=True)
class Connection:
    warehouse: Warehouse
    server: str
    database: str = ""
    schema: str = ""
    role: str = ""
    username: str = ""
    extra: dict[str, str] = field(default_factory=dict)


# Words reserved by ANSI SQL, Snowflake or BigQuery; a column or table with one of these names must be quoted.
RESERVED = frozenset("""all and any array as asc between by case cast collate contains create cross cube current default define desc
distinct else end enum escape except exclude exists extract false fetch following for from full group grouping groups
hash having if ignore in inner intersect interval into is join lateral left like limit lookup merge natural new no not null
nulls of on or order outer over partition preceding proto range recursive respect right rollup rows select set some struct
tablesample then to treat true unbounded union unnest using when where window with within""".split())


def needs_quote(name: str) -> bool:
    return not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) or name.lower() in RESERVED
