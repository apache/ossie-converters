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

import dataclasses
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ossie.models import OssieDocument
from ossie_tableau import warehouse
from ossie_tableau.cli import main
from ossie_tableau.tds_writer import BigQueryConn, SnowflakeConn, write_tds

pytestmark = pytest.mark.unit


def _bigquery_fixture() -> Path:
    return Path(__file__).parent / "fixtures" / "synthetic_bigquery.tds"

DOC = OssieDocument.model_validate({
    "name": "m",
    "datasets": [{"name": "T", "source": "DB.SCH.T", "fields": [
        {"name": "ID", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "ID"}]}, "datatype": "Integer"}]}],
})


def test_registry_and_lookup():
    assert warehouse.get("snowflake") is warehouse.SNOWFLAKE is warehouse.DEFAULT
    assert warehouse.by_tableau_class("snowflake") is warehouse.SNOWFLAKE
    assert warehouse.by_tableau_class("postgres") is None
    assert warehouse.SNOWFLAKE.dialect_preference == ("SNOWFLAKE", "ANSI_SQL")


def test_unknown_warehouse_is_a_clear_error():
    with pytest.raises(ValueError, match="unknown warehouse 'oracle'.*snowflake"):
        warehouse.get("oracle")


def test_snowflake_conn_factory_builds_a_connection():
    c = SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "SCH", "R", "U")
    assert c == warehouse.Connection(warehouse.SNOWFLAKE, "a.snowflakecomputing.com", "DB", "SCH", "R", "U", {"warehouse": "WH"})


def test_snowflake_connection_block():
    xml = write_tds(DOC, SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "SCH", "R", "U")).xml
    conn = ET.fromstring(xml).find(".//named-connection/connection")
    assert dict(conn.attrib) == {
        "authentication": "Username Password", "class": "snowflake", "dbname": "DB", "odbc-connect-string-extras": "",
        "one-time-sql": "", "schema": "SCH", "server": "a.snowflakecomputing.com", "service": "R", "username": "U", "warehouse": "WH"}
    assert conn.find("connection-customization") is None
    assert ET.fromstring(xml).find(".//named-connection").get("name").startswith("snowflake.")


def test_descriptor_drives_the_connection_class():
    pg = dataclasses.replace(warehouse.SNOWFLAKE, name="pg", tableau_class="postgres", sqlglot_dialect="postgres",
                             ossie_dialect="ANSI_SQL", extra_attributes=())
    xml = write_tds(DOC, warehouse.Connection(pg, "h", "DB", "SCH", "R", "U")).xml
    node = ET.fromstring(xml).find(".//named-connection")
    assert node.get("name").startswith("postgres.")
    assert node.find("connection").get("class") == "postgres"
    assert "warehouse" not in node.find("connection").attrib


def test_cli_rejects_unknown_warehouse_type(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["to-tds", "x.yaml", "-o", str(tmp_path), "--warehouse-type", "oracle"])
    assert "invalid choice" in capsys.readouterr().err


def test_bigquery_registry_and_lookup():
    assert warehouse.get("bigquery") is warehouse.BIGQUERY
    assert warehouse.by_tableau_class("bigquery") is warehouse.BIGQUERY
    assert warehouse.BIGQUERY.dialect_preference == ("BIGQUERY", "ANSI_SQL")


def test_bigquery_connection_block():
    xml = write_tds(DOC, BigQueryConn("my-proj", "ds", "bill-proj", "u@example.com")).xml
    node = ET.fromstring(xml).find(".//named-connection")
    assert node.get("name").startswith("bigquery.")
    attrs = dict(node.find("connection").attrib)
    assert attrs["class"] == "bigquery"
    assert attrs["CATALOG"] == "my-proj"
    assert attrs["EXECCATALOG"] == "bill-proj"
    assert attrs["project"] == "my-proj"
    assert attrs["schema"] == "ds"
    assert attrs["connection-dialect"] == "google-sql"
    for omitted in ("dbname", "warehouse", "service", "one-time-sql"):
        assert omitted not in attrs


def test_bigquery_billing_project_defaults_to_data_project():
    c = BigQueryConn("p", "d")
    assert c.extra["EXECCATALOG"] == "p"


def test_bigquery_fixture_translates_to_bigquery_sql():
    from ossie_tableau.convert import convert_file

    (r,) = convert_file(_bigquery_fixture())
    ds = r.document.datasets[0]
    assert ds.source == "synth-project.synth_dataset.ORDERS"
    exprs = {f.name: {d.dialect: d.expression for d in f.expression.dialects} for f in ds.fields}
    assert exprs["Order_Month"]["BIGQUERY"] == "DATETIME_TRUNC(CAST((ORDER_DATE) AS DATETIME), MONTH)"
    assert exprs["Has_Note"]["BIGQUERY"] == "(INSTR((NOTE), 'rush') > 0)"
    assert "SNOWFLAKE" not in {d for e in exprs.values() for d in e}
    assert {x.kind for x in r.losses} >= {"source_qualified", "expression_approximated"}
    assert [m.name for m in r.document.metrics] == ["Total_Amount"]


def test_bigquery_ansi_quoted_identifiers_are_not_string_literals(tmp_path):
    from ossie_tableau.convert import convert_file

    src = _bigquery_fixture().read_text().replace("ORDER_DATE", "Ship Date")
    p = tmp_path / "spaces.tds"
    p.write_text(src)
    (r,) = convert_file(p, warehouse=warehouse.BIGQUERY)
    month = next(f for f in r.document.datasets[0].fields if f.name == "Order_Month")
    bq = next(d.expression for d in month.expression.dialects if d.dialect.value == "BIGQUERY")
    assert "'Ship Date'" not in bq
    assert "Ship Date" in bq


def test_bigquery_round_trip_keeps_every_expression(tmp_path):
    from ossie_tableau.convert import convert_file

    (a,) = convert_file(_bigquery_fixture())
    out = tmp_path / "rt.tds"
    out.write_text(write_tds(a.document, BigQueryConn("synth-project", "synth_dataset")).xml)
    (b,) = convert_file(out)

    def exprs(r):
        return {f.name: sorted((d.dialect, d.expression) for d in f.expression.dialects)
                for f in r.document.datasets[0].fields} | {
            m.name: sorted((d.dialect, d.expression) for d in m.expression.dialects) for m in r.document.metrics}

    assert exprs(a) == exprs(b)
    assert b.document.datasets[0].source == a.document.datasets[0].source


def test_bigquery_categorical_group_escapes_quotes_for_bigquery():
    from ossie_tableau.convert import convert_datasource
    from tests.test_units import BASE, ds

    xml = ("<column name='[G]' caption='Grp' datatype='string' role='dimension'>"
           "<calculation class='categorical-bin' column='[name]' new-bin='true'>"
           "<bin value='&quot;A&quot;'><value>&quot;It&apos;s&quot;</value></bin></calculation></column>")
    r = convert_datasource(ds(BASE, xml), warehouse=warehouse.BIGQUERY)
    by = {d.dialect.value: d.expression for d in next(f for f in r.document.datasets[0].fields if f.name == "Grp").expression.dialects}
    assert "It''s" in by["ANSI_SQL"]
    assert "It\\'s" in by["BIGQUERY"] and "''s" not in by["BIGQUERY"]


def test_bigquery_date_of_us_string_literal_is_rewritten_to_iso():
    from ossie_tableau.calc_parser import to_sql

    assert to_sql('DATE("1/2/2015")', wh=warehouse.BIGQUERY) == "CAST('2015-01-02' AS DATE)"
    assert to_sql('DATE("2015-01-02")', wh=warehouse.BIGQUERY) == "CAST('2015-01-02' AS DATE)"
    assert to_sql("#1/2/2015#", wh=warehouse.BIGQUERY) == "CAST('2015-01-02' AS DATE)"


def test_reserved_word_columns_are_quoted():
    from ossie_tableau.calc_parser import to_sql

    assert to_sql("[Order] + 1", wh=warehouse.BIGQUERY) == '"Order" + 1' or to_sql("[Order] + 1", wh=warehouse.BIGQUERY) == "`Order` + 1"
    assert warehouse.needs_quote("Group") and warehouse.needs_quote("end") and not warehouse.needs_quote("Region")


def test_bigquery_mod_of_floor_is_cast_to_int64():
    from ossie_tableau.calc_parser import to_sql

    out = to_sql("FLOOR([a] / 60) % 60", wh=warehouse.BIGQUERY)
    assert "CAST(" in out and "INT64" in out and "MOD(" in out
    assert "INT64" not in to_sql("[a] % 7", wh=warehouse.BIGQUERY)
    assert "INT64" not in to_sql("FLOOR([a] / 60) % 60", wh=warehouse.SNOWFLAKE)


def test_bigquery_numeric_function_on_text_operand_uses_safe_cast():
    from ossie_tableau.calc_parser import to_sql, translate

    out = to_sql("INT(LEFT([s], 2))", wh=warehouse.BIGQUERY)
    assert "SAFE_CAST(" in out and "FLOAT64" in out
    assert any("SAFE_CAST" in n for n in translate("INT(LEFT([s], 2))", wh=warehouse.BIGQUERY).notes)
    assert "SAFE_CAST" not in to_sql("INT([n])", wh=warehouse.BIGQUERY)
    assert "TRY_CAST" not in to_sql("INT(LEFT([s], 2))", wh=warehouse.SNOWFLAKE)


def test_bigquery_split_negative_token_counts_from_the_end():
    from ossie_tableau.calc_parser import to_sql

    assert "ARRAY_REVERSE(SPLIT(s, '$'))[SAFE_OFFSET(0)]" in to_sql("SPLIT([s], '$', -1)", wh=warehouse.BIGQUERY)
    assert "SAFE_OFFSET(1)" in to_sql("SPLIT([s], '$', -2)", wh=warehouse.BIGQUERY)
    assert "SAFE_OFFSET(3 - 1)" in to_sql("SPLIT([s], '$', 3)", wh=warehouse.BIGQUERY) or "SAFE_OFFSET(2)" in to_sql("SPLIT([s], '$', 3)", wh=warehouse.BIGQUERY)


def test_bigquery_text_operand_in_arithmetic_uses_safe_cast():
    from ossie_tableau.calc_parser import translate

    res = lambda ds, n: (warehouse_col(n), "str")  # noqa: E731

    def warehouse_col(n):
        from sqlglot import exp
        return exp.column(n)

    bq = translate("[a] / 3", res, warehouse.BIGQUERY)
    assert bq.sql == "SAFE_CAST(a AS FLOAT64) / 3" and any("SAFE_CAST" in n for n in bq.notes)
    assert translate("2024 - [a]", res, warehouse.BIGQUERY).sql == "2024 - SAFE_CAST(a AS FLOAT64)"
    assert translate("[a] + 'x'", res, warehouse.BIGQUERY).sql == "a || 'x'"
    assert translate("[a] / 3", res, warehouse.SNOWFLAKE).sql == "a / 3"


def test_bigquery_date_of_text_expression_tries_iso_then_us_format():
    from ossie_tableau.calc_parser import to_sql

    out = to_sql("DATE('12/31/' + STR([y]))", wh=warehouse.BIGQUERY)
    assert out.startswith("COALESCE(SAFE_CAST(") and "SAFE.PARSE_DATE('%m/%d/%Y'" in out
    assert "PARSE_DATETIME" in to_sql("DATETIME([s] + 'x')", wh=warehouse.BIGQUERY)
    assert "COALESCE" not in to_sql("DATE([d])", wh=warehouse.BIGQUERY)
    assert "COALESCE" not in to_sql("DATE('12/31/2015')", wh=warehouse.BIGQUERY)


def test_bigquery_ansi_quoted_column_is_a_physical_column_not_a_string_literal():
    doc = OssieDocument.model_validate({
        "name": "m",
        "datasets": [{"name": "T", "source": "p.d.T", "fields": [
            {"name": "Postal Code", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": '"Postal Code"'}]},
             "datatype": "Integer"}]}],
    })
    xml = write_tds(doc, BigQueryConn("p", "d")).xml
    assert "<remote-name>Postal Code</remote-name>" in xml
    assert "formula=\"'Postal Code'\"" not in xml
