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

from ossie.models import OssieDocument, OssieField
from ossie_tableau.convert import convert_file
from ossie_tableau.losses import KNOWN_KINDS
from ossie_tableau.tds_writer import SnowflakeConn, write_tds

CONN = SnowflakeConn("acct.snowflakecomputing.com", "WH", "DB", "SCH", "R")


def _ex(dialect, e):
    return {"dialects": [{"dialect": dialect, "expression": e}]}


def _doc():
    return OssieDocument.model_validate({
        "name": "star",
        "datasets": [
            {"name": "FACT", "source": "DB.SCH.FACT", "primary_key": ["ID"], "fields": [
                {"name": "ID", "expression": _ex("SNOWFLAKE", "ID"), "datatype": "Integer"},
                {"name": "CUST", "expression": _ex("SNOWFLAKE", "CUST"), "dimension": {}, "datatype": "String", "label": "Customer"},
                {"name": "AMT", "expression": _ex("SNOWFLAKE", "AMT"), "datatype": "Decimal", "description": "Order amount"},
                {"name": "AMT2", "expression": _ex("TABLEAU", "[FACT].[AMT] * 2"), "datatype": "Decimal"},
                {"name": "WEIRD", "expression": _ex("SNOWFLAKE", "REGEXP_SUBSTR(CUST, 'x')"), "dimension": {}},
            ]},
            {"name": "DIM", "source": "DB.SCH.DIM", "primary_key": ["CUST"], "fields": [
                {"name": "CUST", "expression": _ex("SNOWFLAKE", "CUST"), "dimension": {}, "datatype": "String"},
                {"name": "REGION", "expression": _ex("SNOWFLAKE", "REGION"), "dimension": {}, "datatype": "String"},
            ]},
        ],
        "relationships": [{"name": "r", "from": "FACT", "to": "DIM", "from_columns": ["CUST"], "to_columns": ["CUST"]}],
        "metrics": [
            {"name": "total", "expression": _ex("SNOWFLAKE", "SUM(FACT.AMT)")},
            {"name": "ratio", "expression": _ex("SNOWFLAKE", "SUM(FACT.AMT) / COUNT(DISTINCT FACT.CUST)")},
            {"name": "star", "expression": _ex("SNOWFLAKE", "COUNT(*)")},
            {"name": "native", "expression": _ex("TABLEAU", "AVG([FACT].[AMT])")},
        ],
    })


@pytest.mark.unit
def test_well_formed_and_deterministic():
    a, b = write_tds(_doc(), CONN), write_tds(_doc(), CONN)
    ET.fromstring(a.xml)
    assert a.xml == b.xml


@pytest.mark.unit
def test_losses_are_classified_not_silent():
    kinds = {(l.kind, l.name) for l in write_tds(_doc(), CONN).losses}
    assert ("untranslatable_expression", "FACT.WEIRD") in kinds
    assert ("untranslatable_expression", "star") not in kinds
    assert {k for k, _ in kinds} <= KNOWN_KINDS


@pytest.mark.unit
def test_formulas_use_local_names():
    root = ET.fromstring(write_tds(_doc(), CONN).xml)
    formulas = {c.get("caption"): c.find("calculation").get("formula") for c in root.findall("column") if c.find("calculation") is not None}
    assert formulas["total"] == "SUM([AMT])"
    assert formulas["ratio"] == "SUM([AMT]) / COUNTD([CUST])"
    assert formulas["native"] == "AVG([AMT])"
    assert formulas["AMT2"] == "[AMT] * 2"
    assert formulas["star"] == "COUNT(*)"


@pytest.mark.unit
def test_fixed_point(tmp_path):
    doc = _doc()
    p = tmp_path / "x.tds"
    p.write_text(write_tds(doc, CONN).xml)
    (r,) = convert_file(p)
    d = r.document
    assert [x.name for x in d.datasets] == ["FACT", "DIM"]
    assert {f.name for f in d.datasets[0].fields} == {"ID", "CUST", "AMT", "AMT2"}
    assert [(x.from_dataset, x.to, x.from_columns, x.to_columns) for x in d.relationships] == [("FACT", "DIM", ["CUST"], ["CUST"])]
    assert d.datasets[1].primary_key == ["CUST"]
    assert d.datasets[1].unique_keys is None
    assert {m.name for m in d.metrics} == {"total", "ratio", "native", "star"}
    assert d.datasets[0].source == "DB.SCH.FACT"


@pytest.mark.unit
def test_undeclared_relationship_key_becomes_hidden_column():
    doc = OssieDocument.model_validate({
        "name": "m",
        "datasets": [{"name": "F", "source": "DB.SCH.F", "fields": [{"name": "A", "expression": _ex("ANSI_SQL", "A"), "dimension": {}}]},
                     {"name": "D", "source": "DB.SCH.D", "primary_key": ["K"],
                      "fields": [{"name": "B", "expression": _ex("ANSI_SQL", "B"), "dimension": {}}]}],
        "relationships": [{"name": "r", "from": "F", "to": "D", "from_columns": ["K"], "to_columns": ["K"]}],
    })
    res = write_tds(doc, CONN)
    root = ET.fromstring(res.xml)
    assert len(list(root.iter("relationship"))) == 1
    assert [c.get("name") for c in root.iter("column") if c.get("hidden") == "true"] == ["[K]", "[K (D)]"]
    assert {"relationship_key_synthesized"} <= {l.kind for l in res.losses} <= KNOWN_KINDS | {"datatype_defaulted"}


@pytest.mark.unit
def test_qualified_sql_becomes_source_column():
    """Snowflake READ emits table.column; the Ossie field name is often a label."""
    doc = OssieDocument.model_validate({
        "name": "sv",
        "datasets": [{"name": "CUSTOMERS", "source": "SUPERSTORE.AGENTS.CUSTOMERS", "primary_key": ["CUSTOMER_ID"],
                      "fields": [
                          {"name": "CUSTOMER_ID", "expression": _ex("SNOWFLAKE", "customers.CUSTOMER_ID"),
                           "dimension": {}, "datatype": "String"},
                          {"name": "CUSTOMER_ACCOUNT_NAME", "expression": _ex("SNOWFLAKE", "customers.CUSTOMER_NAME"),
                           "dimension": {}, "datatype": "String", "label": "Customer"},
                      ]}],
        "metrics": [{"name": "UNIQUE_CUSTOMERS", "expression": _ex("SNOWFLAKE", "COUNT(DISTINCT customers.CUSTOMER_ID)")}],
    })
    res = write_tds(doc, CONN)
    kinds = {l.kind for l in res.losses}
    assert "untranslatable_expression" not in kinds
    root = ET.fromstring(res.xml)
    remotes = {r.findtext("remote-name") for r in root.iter("metadata-record") if r.get("class") == "column"}
    assert remotes == {"CUSTOMER_ID", "CUSTOMER_NAME"}
    captions = {c.get("caption") or c.get("name") for c in root.findall("column")}
    assert "Customer" in captions or "CUSTOMER_ACCOUNT_NAME" in captions
    formulas = {c.get("caption"): (c.find("calculation").get("formula") if c.find("calculation") is not None else None)
                for c in root.findall("column")}
    assert formulas["UNIQUE_CUSTOMERS"] == "COUNTD([CUSTOMER_ID])"


@pytest.mark.unit
def test_metric_can_reference_another_metric():
    doc = OssieDocument.model_validate({
        "name": "m",
        "datasets": [{"name": "ORDERS", "source": "SUPERSTORE.AGENTS.ORDER_LINES", "primary_key": ["ORDER_ID"],
                      "fields": [
                          {"name": "ORDER_ID", "expression": _ex("SNOWFLAKE", "orders.ORDER_ID"), "dimension": {}, "datatype": "String"},
                          {"name": "SALES_AMOUNT", "expression": _ex("SNOWFLAKE", "orders.SALES"), "datatype": "Decimal"},
                      ]}],
        "metrics": [
            {"name": "TOTAL_SALES", "expression": _ex("SNOWFLAKE", "SUM(orders.SALES)")},
            {"name": "AVERAGE_ORDER_VALUE", "expression": _ex("SNOWFLAKE", "SUM(orders.SALES) / NULLIF(orders.total_sales, 0)")},
        ],
    })
    res = write_tds(doc, CONN)
    assert "untranslatable_expression" not in {l.kind for l in res.losses}
    root = ET.fromstring(res.xml)
    formulas = {c.get("caption"): c.find("calculation").get("formula") for c in root.findall("column") if c.find("calculation") is not None}
    assert formulas["TOTAL_SALES"] == "SUM([SALES])"
    assert "TOTAL_SALES" in formulas["AVERAGE_ORDER_VALUE"] or "SALES" in formulas["AVERAGE_ORDER_VALUE"]


_CUMULATIVE = """SUM(orders.total_sales) OVER (
        PARTITION BY EXCLUDING orders.order_date
        ORDER BY orders.order_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      )"""
_MOVING = """AVG(orders.total_sales) OVER (
        PARTITION BY EXCLUDING orders.order_date
        ORDER BY orders.order_date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
      )"""
_LAG = """orders.total_profit - LAG(orders.total_profit) OVER (
        PARTITION BY EXCLUDING orders.order_month
        ORDER BY orders.order_month
      )"""


def _window_doc():
    return OssieDocument.model_validate({
        "name": "w",
        "datasets": [{"name": "ORDERS", "source": "SUPERSTORE.AGENTS.ORDER_LINES", "primary_key": ["ORDER_ID"],
                      "fields": [
                          {"name": "ORDER_ID", "expression": _ex("SNOWFLAKE", "orders.ORDER_ID"), "dimension": {}, "datatype": "String"},
                          {"name": "SALES_AMOUNT", "expression": _ex("SNOWFLAKE", "orders.SALES"), "datatype": "Decimal"},
                          {"name": "PROFIT_AMOUNT", "expression": _ex("SNOWFLAKE", "orders.PROFIT"), "datatype": "Decimal"},
                      ]}],
        "metrics": [
            {"name": "TOTAL_SALES", "expression": _ex("SNOWFLAKE", "SUM(orders.SALES)")},
            {"name": "TOTAL_PROFIT", "expression": _ex("SNOWFLAKE", "SUM(orders.PROFIT)")},
            {"name": "CUMULATIVE_SALES", "expression": _ex("SNOWFLAKE", _CUMULATIVE)},
            {"name": "MOVING_AVG_SALES_7D", "expression": _ex("SNOWFLAKE", _MOVING)},
            {"name": "PROFIT_VS_PREVIOUS_MONTH", "expression": _ex("SNOWFLAKE", _LAG)},
        ],
    })


@pytest.mark.unit
def test_excluding_window_to_tableau_shapes():
    from ossie_tableau.tds_writer import _excluding_window_to_tableau
    resolve = lambda ident: "[" + ident.rsplit(".", 1)[-1].upper() + "]"
    assert _excluding_window_to_tableau(_CUMULATIVE, resolve) == "RUNNING_SUM([TOTAL_SALES])"
    assert _excluding_window_to_tableau(_MOVING, resolve) == "WINDOW_AVG([TOTAL_SALES], -6, 0)"
    assert _excluding_window_to_tableau(_LAG, resolve) == "[TOTAL_PROFIT] - LOOKUP([TOTAL_PROFIT], -1)"
    assert _excluding_window_to_tableau("SUM(a) OVER (ORDER BY b)", resolve) is None


@pytest.mark.unit
def test_excluding_windows_write_as_table_calcs():
    res = write_tds(_window_doc(), CONN)
    kinds = {l.kind for l in res.losses}
    assert "untranslatable_expression" not in kinds
    assert "table_calc_translated" in kinds
    root = ET.fromstring(res.xml)
    formulas = {c.get("caption"): c.find("calculation").get("formula")
                for c in root.findall("column") if c.find("calculation") is not None}
    assert formulas["CUMULATIVE_SALES"].startswith("RUNNING_SUM(")
    assert formulas["MOVING_AVG_SALES_7D"].startswith("WINDOW_AVG(")
    assert "LOOKUP(" in formulas["PROFIT_VS_PREVIOUS_MONTH"]


@pytest.mark.unit
def test_excluding_windows_roundtrip_keep_all_metrics(tmp_path):
    res = write_tds(_window_doc(), CONN)
    path = tmp_path / "w.tds"
    path.write_text(res.xml)
    back = convert_file(path)[0]
    names = {m.name for m in (back.document.metrics or [])}
    assert {"TOTAL_SALES", "TOTAL_PROFIT", "CUMULATIVE_SALES", "MOVING_AVG_SALES_7D", "PROFIT_VS_PREVIOUS_MONTH"} <= names
    assert {l.kind for l in back.losses} & {"untranslatable_expression", "row_level_calc", "table_calc", "aggregate_over_expression"} == set()


@pytest.mark.unit
def test_composed_metric_roundtrip_is_not_row_level(tmp_path):
    doc = OssieDocument.model_validate({
        "name": "m",
        "datasets": [{"name": "ORDERS", "source": "SUPERSTORE.AGENTS.ORDER_LINES", "primary_key": ["ORDER_ID"],
                      "fields": [
                          {"name": "ORDER_ID", "expression": _ex("SNOWFLAKE", "orders.ORDER_ID"), "dimension": {}, "datatype": "String"},
                          {"name": "SALES_AMOUNT", "expression": _ex("SNOWFLAKE", "orders.SALES"), "datatype": "Decimal"},
                      ]}],
        "metrics": [
            {"name": "TOTAL_SALES", "expression": _ex("SNOWFLAKE", "SUM(orders.SALES)")},
            {"name": "AVERAGE_ORDER_VALUE", "expression": _ex("SNOWFLAKE", "SUM(orders.SALES) / NULLIF(orders.total_sales, 0)")},
        ],
    })
    res = write_tds(doc, CONN)
    path = tmp_path / "m.tds"
    path.write_text(res.xml)
    back = convert_file(path)[0]
    names = {m.name for m in (back.document.metrics or [])}
    assert "TOTAL_SALES" in names and "AVERAGE_ORDER_VALUE" in names
    assert "row_level_calc" not in {l.kind for l in back.losses}


@pytest.mark.unit
def test_sql_only_field_becomes_tableau_calc():
    doc = _doc()
    doc.datasets[0].fields.append(OssieField.model_validate(
        {"name": "UP", "datatype": "String", "dimension": {}, "expression": _ex("SNOWFLAKE", "UPPER(CUST)")}))
    r = write_tds(doc, CONN)
    assert ("untranslatable_expression", "FACT.UP") not in {(l.kind, l.name) for l in r.losses}
    root = ET.fromstring(r.xml)
    formulas = {c.get("caption"): c.find("calculation").get("formula") for c in root.findall("column") if c.find("calculation") is not None}
    assert formulas["UP"] == "UPPER([CUST])"


@pytest.mark.unit
@pytest.mark.parametrize("src,out", [
    ("(SELECT * FROM t WHERE id IN (1,2))", "SELECT * FROM t WHERE id IN (1,2)"),
    ("((SELECT 1))", "(SELECT 1)"),
    ("(SELECT 1) UNION (SELECT 2)", "(SELECT 1) UNION (SELECT 2)"),
    ("(SELECT ')' )", "SELECT ')' "),
    ("(SELECT 1 -- )\n)", "SELECT 1 -- )\n"),
    ("(SELECT 1", "(SELECT 1"),
    ("SELECT 1", "SELECT 1"),
    ("(SELECT COUNT(*) FROM (t))", "SELECT COUNT(*) FROM (t)"),
])
def test_unwrap_parens(src, out):
    from ossie_tableau.tds_writer import _unwrap_parens
    assert _unwrap_parens(src) == out


@pytest.mark.unit
def test_unwrap_parens_inverts_wrapping():
    from hypothesis import given, strategies as st
    from ossie_tableau.tds_writer import _unwrap_parens
    balanced = st.recursive(st.sampled_from(["a", "1, 2", "'x'", ""]), lambda c: st.tuples(c, c).map(lambda t: f"{t[0]}({t[1]}){t[0]}"), max_leaves=6)

    @given(balanced)
    def check(s):
        assert _unwrap_parens(f"({s})") == s
    check()


@pytest.mark.unit
def test_custom_sql_source_ending_in_paren_survives_write_and_reconvert(tmp_path):
    sql = "SELECT * FROM t WHERE id IN (1,2)"
    d = OssieDocument.model_validate({"name": "q", "datasets": [{"name": "Q", "source": f"({sql})", "fields": [
        {"name": "id", "expression": _ex("SNOWFLAKE", "id"), "datatype": "Integer"}]}]})
    res = write_tds(d, CONN)
    rel = next(e for e in ET.fromstring(res.xml).iter("relation") if e.get("type") == "text")
    assert rel.text == sql
    p = tmp_path / "q.tds"
    p.write_text(res.xml)
    (r,) = convert_file(p)
    assert r.document.datasets[0].source == sql
    p.write_text(write_tds(r.document, CONN).xml)
    assert next(e for e in ET.parse(p).getroot().iter("relation") if e.get("type") == "text").text == sql
