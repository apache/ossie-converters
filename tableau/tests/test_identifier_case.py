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

import xml.etree.ElementTree as ET

import pytest
import sqlglot
from hypothesis import given, settings, strategies as st
from sqlglot import exp

from ossie_tableau.convert import _sql_col, convert_datasource
from ossie_tableau.tds_writer import SnowflakeConn, write_tds
from ossie_tableau.warehouse import BIGQUERY, GENERIC, SNOWFLAKE, needs_quote

from .test_units import calc, recs

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name,sf,bq", [
    ("OrderId", True, False),
    ("order_id", True, False),
    ("ORDER_ID", False, False),
    ("_X1", False, False),
    ("order", True, True),  # reserved everywhere
    ("ORDER", True, True),
    ("odd col", True, True),
    ("1st", True, True),
])
def test_needs_quote_follows_the_warehouse_fold(name, sf, bq):
    assert needs_quote(name, SNOWFLAKE) is sf
    assert needs_quote(name, BIGQUERY) is bq
    assert needs_quote(name, GENERIC) is bq
    assert needs_quote(name) is bq  # no warehouse: spelling-only rule


def _resolved(sql: str, dialect: str, fold_upper: bool) -> str:
    """The column name the warehouse looks up for `sql` used as a select item."""
    col = sqlglot.parse_one(f"SELECT {sql} FROM t", read=dialect).selects[0]
    assert isinstance(col, exp.Column), (sql, col)
    return col.this.name if col.this.quoted or not fold_upper else col.this.name.upper()


@pytest.mark.parametrize("word", sorted(k for k in sqlglot.tokens.Tokenizer.KEYWORDS if k.replace("_", "").isalpha()))
def test_every_sqlglot_keyword_column_resolves_to_itself(word):
    for wh, fold in ((SNOWFLAKE, True), (GENERIC, False)):
        assert _resolved(_sql_col(word, wh), "snowflake" if fold else "", fold) == word, wh.name


@settings(max_examples=300, deadline=None)
@given(st.from_regex(r"[A-Za-z_][A-Za-z0-9_ \"]{0,12}", fullmatch=True))
def test_quoted_column_resolves_to_its_exact_name(name):
    for wh, fold in ((SNOWFLAKE, True), (GENERIC, False)):
        assert _resolved(_sql_col(name, wh), "snowflake" if fold else "", fold) == name, (wh.name, name)
        assert needs_quote(name, wh) == needs_quote(name, wh)


def _mixed(cls: str) -> ET.Element:
    return ET.fromstring(
        f"<datasource name='d'><connection class='{cls}' dbname='DB' schema='PUBLIC'>"
        "<relation connection='c' name='OrderLines' table='[PUBLIC].[OrderLines]' type='table'/>"
        + recs("OrderLines", [("OrderId", "integer"), ("AMOUNT", "real"), ("region", "string")])
        + "</connection>" + calc("SUM([OrderId]) + SUM([AMOUNT])") + "</datasource>")


def _field_sql(r) -> dict[str, str]:
    return {f.name: f.expression.dialects[0].expression for d in r.document.datasets for f in d.fields}


def test_snowflake_mixed_case_columns_and_tables_are_quoted():
    r = convert_datasource(_mixed("snowflake"))
    (d,) = r.document.datasets
    assert d.source == 'DB.PUBLIC."OrderLines"'
    assert _field_sql(r) == {"OrderId": '"OrderId"', "AMOUNT": "AMOUNT", "region": '"region"'}
    metric = next(x.expression for x in r.document.metrics[0].expression.dialects if x.dialect.value == "SNOWFLAKE")
    assert '"OrderId"' in metric and '"AMOUNT"' not in metric


def test_bigquery_mixed_case_columns_stay_bare():
    r = convert_datasource(_mixed("bigquery"))
    assert r.document.datasets[0].source == "DB.PUBLIC.OrderLines"
    assert _field_sql(r) == {"OrderId": "OrderId", "AMOUNT": "AMOUNT", "region": "region"}


def test_quoted_names_round_trip_through_to_tds():
    r = convert_datasource(_mixed("snowflake"))
    root = ET.fromstring(write_tds(r.document, SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "PUBLIC", "R")).xml)
    rel = next(e for e in root.iter() if e.tag.endswith("relation") and e.get("type") == "table")
    assert rel.get("table") == "[DB].[PUBLIC].[OrderLines]"
    remotes = {e.text for e in root.iter() if e.tag.endswith("remote-name")}
    assert {"OrderId", "AMOUNT", "region"} <= remotes
