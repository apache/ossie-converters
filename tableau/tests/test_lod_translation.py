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

import re

import pytest

from ossie_tableau.calc_parser import Untranslatable, from_sql, translate

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("formula,sql,back", [
    ("{FIXED [REGION] : SUM([amt])}", "SUM(amt) OVER (PARTITION BY REGION)", "{FIXED [REGION] : SUM([amt])}"),
    ("{FIXED [a], [b] : AVG([x])}", "AVG(x) OVER (PARTITION BY a, b)", "{FIXED [a], [b] : AVG([x])}"),
    ("{SUM([amt])}", "SUM(amt) OVER ()", "{FIXED : SUM([amt])}"),
    ("{FIXED : MAX([amt])}", "MAX(amt) OVER ()", "{FIXED : MAX([amt])}"),
    ("TOTAL(SUM([amt]))", "SUM(amt) OVER ()", "{FIXED : SUM([amt])}"),
    ("TOTAL(AVG([amt]))", "AVG(amt) OVER ()", "{FIXED : AVG([amt])}"),
    ("WINDOW_SUM(SUM([amt]))", "SUM(amt) OVER ()", "{FIXED : SUM([amt])}"),
    ("WINDOW_SUM(COUNT([amt]))", "COUNT(amt) OVER ()", "{FIXED : COUNT([amt])}"),
    ("WINDOW_MIN(MIN([amt]))", "MIN(amt) OVER ()", "{FIXED : MIN([amt])}"),
    ("WINDOW_MAX(MAX([amt]))", "MAX(amt) OVER ()", "{FIXED : MAX([amt])}"),
])
def test_proven_shapes(formula, sql, back):
    t = translate(formula)
    assert t.sql == sql and t.notes
    assert from_sql(sql) == back


@pytest.mark.parametrize("formula", [
    "WINDOW_AVG(SUM([a]))", "WINDOW_SUM(AVG([a]))",
    "WINDOW_AVG(AVG([a]))", "WINDOW_MIN(MAX([a]))", "WINDOW_MAX(MIN([a]))",
])
def test_window_mismatched_pairs_translate(formula):
    t = translate(formula)
    assert "OVER" in t.sql
    assert any("addressing" in n for n in t.notes)


@pytest.mark.parametrize("formula", ["WINDOW_SUM(SUM([amt]))", "WINDOW_MIN(MIN([amt]))", "TOTAL(MAX([amt]))"])
def test_window_exact_pairs_translate(formula):
    assert " OVER ()" in translate(formula).sql
    assert "lod_translated" in {x.kind for x in _convert_calc(formula).losses}


@pytest.mark.parametrize("formula", ["WINDOW_AVG(SUM([amt]))", "WINDOW_SUM(AVG([amt]))"])
def test_convert_wrong_pairs_are_tableau_metrics(formula):
    r = _convert_calc(formula)
    assert len(r.document.metrics) == 1
    ks = {x.kind for x in r.losses}
    assert not ks & {"lod", "table_calc", "lod_translated", "table_calc_translated", "metric_unsupported_binding"}


@pytest.mark.parametrize("formula", ["RUNNING_SUM(SUM([a]))", "RUNNING_AVG(SUM([a]))"])
def test_running_family_stays_untranslatable(formula):
    with pytest.raises(Untranslatable):
        translate(formula)


@pytest.mark.parametrize("formula,needle", [
    ("WINDOW_COUNT(SUM([a]))", "COUNT"),
    ("RANK_PERCENTILE(SUM([a]))", "PERCENT_RANK"),
    ("LOOKUP(SUM([a]), FIRST())", "FIRST_VALUE"),
    ("INDEX()>1", "ROW_NUMBER"),
    ("LOOKUP(SUM([a]), -1) IS NULL", "LAG"),
])
def test_parked_families_now_translate(formula, needle):
    t = translate(formula)
    assert needle in t.sql.upper() or needle in t.sql


@pytest.mark.parametrize("formula,needle", [
    ("{INCLUDE [r] : SUM([a])}", "PARTITION BY"),
    ("{EXCLUDE [r] : SUM([a])}", "OVER ()"),
    ("{FIXED [r] : [a]}", "FIRST_VALUE"),
    ("WINDOW_SUM(SUM([a]), -2, 0)", "PRECEDING"),
    ("RANK(SUM([a]))", "RANK()"),
    ("LOOKUP(SUM([a]), -1)", "LAG"),
    ("INDEX()", "ROW_NUMBER"),
    ("PREVIOUS_VALUE(SUM([a]))", "FIRST_VALUE"),
    ("FIRST()", "ROW_NUMBER"),
    ("LAST()", "COUNT"),
    ("SIZE()", "COUNT"),
    ("INDEX()+1", "ROW_NUMBER"),
    ("INDEX()+RANK(SUM([a]))", "RANK()"),
    ("(INDEX()+1)*SIZE()", "ROW_NUMBER"),
    ("WINDOW_SUM(AVG([a]))", "OVER"),
    ("IIF(INDEX()>0, 1, 0)", "IFF"),
    ("IF FIRST()=0 THEN SUM([a]) END", "CASE"),
])
def test_viz_dependent_shapes_translate(formula, needle):
    t = translate(formula)
    assert needle in t.sql
    assert any("addressing" in n for n in t.notes)


def _convert_calc(formula):
    from ossie_tableau.convert import convert_datasource
    from .test_units import BASE, calc, ds
    return convert_datasource(ds(BASE, calc(formula)))


@pytest.mark.parametrize("formula", ["TOTAL(SUM([amt]))"])
def test_convert_emits_whole_formula_window_metric(formula):
    r = _convert_calc(formula)
    assert len(r.document.metrics) == 1
    assert "lod_translated" in {x.kind for x in r.losses}
    assert not {"lod", "table_calc"} & {x.kind for x in r.losses}


@pytest.mark.parametrize("formula", ["{SUM([amt])}", "{FIXED : SUM([amt])}"])
def test_convert_parks_brace_lod_metric(formula):
    r = _convert_calc(formula)
    assert len(r.document.metrics) == 1
    ks = {x.kind for x in r.losses}
    assert not ks & {"lod", "metric_unsupported_binding", "lod_translated", "sql_unreconciled"}
    assert "{" in r.document.metrics[0].expression.dialects[0].expression


@pytest.mark.parametrize("formula", [
    "{INCLUDE [amt] : SUM([amt])}", "{EXCLUDE [amt] : SUM([amt])}", "RANK(SUM([amt]))",
    "RANK_DENSE(SUM([amt]))", "RANK_UNIQUE(SUM([amt]))", "WINDOW_SUM(SUM([amt]), -2, 0)",
    "INDEX()", "PREVIOUS_VALUE(SUM([amt]))", "PREVIOUS_VALUE(0)", "{FIXED [amt] : [amt]}",
    "FIRST()", "LAST()", "SIZE()", "INDEX()+1", "SIZE()*2", "1+INDEX()",
    "{FIXED [amt] : [amt]} * 2", "{INCLUDE [amt] : SUM([amt])} + 1",
    "LOOKUP(SUM([amt]), -1)", "LOOKUP(AVG([amt]), -2)",
    "INDEX()+RANK(SUM([amt]))", "FIRST()-LAST()",
    "(INDEX()+1)*SIZE()", "(FIRST()-1)+SIZE()",
    "WINDOW_SUM(AVG([amt]))", "WINDOW_AVG(SUM([amt]))", "WINDOW_AVG(AVG([amt]))",
    "IIF(INDEX()>0, 1, 0)", "IF FIRST()=0 THEN SUM([amt]) END",
    "{FIXED [amt]: SUM([amt])} * 2", "{FIXED [amt]: AVG([amt])} + 1",
    "INDEX()>1", "LOOKUP(SUM([amt]), -1) IS NULL",
    "WINDOW_COUNT(SUM([amt]))",
    "RANK_PERCENTILE(SUM([amt]))", "LOOKUP(SUM([amt]), FIRST())",
])
def test_convert_keeps_viz_dependent_shapes_as_losses(formula):
    r = _convert_calc(formula)
    ks = {x.kind for x in r.losses}
    assert len(r.document.metrics) == 1
    assert not ks & {"lod", "table_calc", "lod_translated", "table_calc_translated", "metric_unsupported_binding"}
    quiet = formula in (
        "SIZE()", "SIZE()*2", "INDEX()", "INDEX()+1", "1+INDEX()",
        "FIRST()", "LAST()", "FIRST()-LAST()", "(INDEX()+1)*SIZE()", "(FIRST()-1)+SIZE()",
        "RANK(SUM([amt]))", "RANK_DENSE(SUM([amt]))", "RANK_UNIQUE(SUM([amt]))",
        "LOOKUP(SUM([amt]), -1)", "LOOKUP(AVG([amt]), -2)",
        "INDEX()+RANK(SUM([amt]))",
        "IIF(INDEX()>0, 1, 0)", "IF FIRST()=0 THEN SUM([amt]) END",
        "{FIXED [amt] : [amt]}", "{FIXED [amt] : [amt]} * 2",
        "INDEX()>1", "LOOKUP(SUM([amt]), -1) IS NULL",
        "WINDOW_COUNT(SUM([amt]))",
        "RANK_PERCENTILE(SUM([amt]))", "LOOKUP(SUM([amt]), FIRST())",
    ) or formula.startswith("{FIXED [amt]: SUM") or formula.startswith("{FIXED [amt]: AVG")
    if quiet:
        assert "sql_unreconciled" not in ks
    else:
        assert "sql_unreconciled" in ks
    dialects = {str(getattr(d.dialect, "value", d.dialect)) for d in r.document.metrics[0].expression.dialects}
    assert "TABLEAU" in dialects and "SNOWFLAKE" in dialects and "ANSI_SQL" in dialects and "BIGQUERY" in dialects


@pytest.mark.parametrize("formula,kind", [
    ("RUNNING_SUM(SUM([amt]))", "RUNNING_SUM"),
    ("WINDOW_AVG(SUM([amt]), -6, 0)", "WINDOW_AVG"),
    ("SUM([amt]) - LOOKUP(SUM([amt]), -1)", "LOOKUP"),
])
def test_convert_narrow_table_calcs_are_metrics(formula, kind):
    r = _convert_calc(formula)
    assert len(r.document.metrics) == 1
    assert "table_calc" not in {x.kind for x in r.losses}
    assert "table_calc_translated" not in {x.kind for x in r.losses}
    assert kind in r.document.metrics[0].expression.dialects[0].expression


def test_convert_window_metric_carries_tableau_and_sql_dialects():
    r = _convert_calc("TOTAL(SUM([amt]))")
    ex = {d.dialect.value if hasattr(d.dialect, "value") else d.dialect: d.expression for d in r.document.metrics[0].expression.dialects}
    assert ex["TABLEAU"] == "TOTAL(SUM([orders].[amt]))" or "TOTAL" in ex["TABLEAU"]
    assert any("OVER" in e for k, e in ex.items() if k != "TABLEAU")


def test_convert_single_dataset_fixed_becomes_field_with_window_sql():
    r = _convert_calc("{FIXED [name] : SUM([amt])}")
    fs = {f.name: f for f in r.document.datasets[0].fields}
    f = next(v for k, v in fs.items() if k not in ("amt", "name", "when", "odd_col", "odd col"))
    ex = {str(getattr(d.dialect, "value", d.dialect)): d.expression for d in f.expression.dialects}
    assert "FIXED" in ex["TABLEAU"] and "OVER" in ex["SNOWFLAKE"] and "PARTITION BY" in ex["SNOWFLAKE"]
    assert "lod_translated" in {x.kind for x in r.losses} and not r.document.metrics


def test_fixed_field_round_trips_byte_stable():
    import xml.etree.ElementTree as ET
    from ossie_tableau.convert import convert_datasource
    from ossie_tableau.tds_writer import SnowflakeConn, write_tds
    from .test_units import BASE, calc, ds
    conn = SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "SCH", "R", "U")
    r = convert_datasource(ds(BASE, calc("{FIXED [name] : SUM([amt])}")))
    x1 = write_tds(r.document, conn).xml
    assert "FIXED" in x1
    x2 = write_tds(convert_datasource(ET.fromstring(x1)).document, conn).xml
    assert x2 == x1


def test_first_is_one_minus_row_number():
    sql = re.sub(r"\s+", "", translate("FIRST()").sql)
    assert sql == "1-ROW_NUMBER()OVER()"


def test_rank_unwraps_sum_and_brace_has_no_order():
    rank = translate("RANK(SUM([a]))").sql.upper()
    assert "RANK()" in rank and "SUM" not in rank
    assert "ORDER BY" not in translate("{SUM([a])}").sql.upper()
    assert "ORDER BY" not in translate("SIZE()").sql.upper()


def test_previous_value_is_first_value():
    from sqlglot import exp
    t = translate("PREVIOUS_VALUE(SUM([a]))", order=[exp.column("id")])
    sql = t.sql.upper()
    assert "FIRST_VALUE" in sql and "ORDER BY" in sql and "LAG" not in sql


def test_mismatched_window_is_two_stage():
    sql = translate("WINDOW_SUM(AVG([a]))").sql.upper()
    assert "SUM" in sql and sql.count("OVER") == 1
    sql2 = translate("WINDOW_AVG(SUM([a]))").sql.upper()
    assert "AVG" in sql2 and sql2.count("OVER") == 1


def test_fixed_nonagg_is_first_value():
    from sqlglot import exp
    t = translate("{FIXED [r] : [a]}", order=[exp.column("id")])
    sql = t.sql.upper()
    assert "FIRST_VALUE" in sql and "PARTITION BY" in sql and "MAX(" not in sql
    assert "ORDER BY" in sql


def test_lag_unwraps_and_takes_order():
    from sqlglot import exp
    t = translate("LOOKUP(SUM([a]), -1)", order=[exp.column("id")])
    sql = t.sql.upper()
    assert "LAG" in sql and "ORDER BY" in sql and "SUM" not in sql
    rows = translate("WINDOW_SUM(SUM([a]), -2, 0)", order=[exp.column("id")]).sql.upper()
    assert "ORDER BY" in rows and "PRECEDING" in rows


def test_table_calc_addressing_reads_ordering_field():
    import xml.etree.ElementTree as ET
    from ossie_tableau.convert import table_calc_addressing, table_calc_meta
    root = ET.fromstring("""
    <workbook>
      <worksheets>
        <worksheet>
          <table-calc field='[Calculation_1]' ordering-type='Field'
                      ordering-field='[SAMPLE_SUPERSTORE].[Order ID]' />
          <table-calc ordering-field='[ds].[Region]' />
          <table-calc field='[Calculation_2]' ordering-type='Columns' />
        </worksheet>
      </worksheets>
    </workbook>""")
    addr = table_calc_addressing(root)
    assert addr["Calculation_1"] == ["Order ID"]
    assert addr["*"] == ["Region"]
    keys, types = table_calc_meta(root)
    assert keys["Calculation_1"] == ["Order ID"]
    assert types["Calculation_1"] == "Field"
    assert types["Calculation_2"] == "Columns"
    assert "Calculation_2" not in keys


def test_tableau_to_sql_translations():
    from ossie_tableau.calc_parser import tableau_to_sql

    fs = {("T", "QTY"): "QUANTITY", ("T", "PRICE"): "UNIT_PRICE"}
    assert tableau_to_sql("[QTY] * [PRICE]", fs, "T") == "(QUANTITY) * (UNIT_PRICE)"
    assert tableau_to_sql("SUM([T].[QTY])", fs, None) == "SUM(T.QTY)"
    assert tableau_to_sql("COUNTD([T].[QTY])", fs, None) == "COUNT(DISTINCT T.QTY)"
    assert tableau_to_sql("IF [QTY] > 1 THEN 1 END", fs, "T") == "CASE WHEN (QUANTITY) > 1 THEN 1 END"
    assert "OVER" in (tableau_to_sql("{INCLUDE [QTY] : SUM([T].[QTY])}", fs, None) or "")
    assert tableau_to_sql("SOMEFUNC([QTY])", fs, "T") is None
    assert tableau_to_sql("[MISSING] + 1", fs, "T") is None
