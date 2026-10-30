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

"""Phase 1 remediations: stash-and-restore of names, datatypes and metric metadata (TDS -> Ossie -> TDS)."""
import re
import xml.etree.ElementTree as ET

import pytest

from ossie_tableau.convert import convert_datasource
from ossie_tableau.losses import KNOWN_KINDS
from ossie_tableau.tds_writer import SnowflakeConn, write_tds
from .test_units import BASE, ds, kinds, rel, recs

pytestmark = pytest.mark.unit
CONN = SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "SCH", "R", "U")


def _roundtrip(body, cols=""):
    r = convert_datasource(ds(body, cols))
    res = write_tds(r.document, CONN)
    assert {x.kind for x in res.losses} <= KNOWN_KINDS
    return r, res, ET.fromstring(res.xml)


def _col(root, name):
    return next((c for c in root.iter("column") if c.get("name") == f"[{name}]"), None)


def test_plain_fields_carry_no_names_stash():
    r = convert_datasource(ds(BASE))
    assert all(not f.custom_extensions for f in r.document.datasets[0].fields)


def test_physical_caption_and_local_name_restored():
    body = rel() + (
        "<metadata-records><metadata-record class='column'><remote-name>amt</remote-name><local-name>[Amount (orders)]</local-name>"
        "<parent-name>[orders]</parent-name><local-type>real</local-type></metadata-record></metadata-records>")
    _, _, root = _roundtrip(body, "<column name='[Amount (orders)]' caption='Order Amount' datatype='real' role='measure'/>")
    c = _col(root, "Amount (orders)")
    assert c is not None and c.get("caption") == "Order Amount"


def test_calc_local_name_and_caption_restored_byte_stable():
    cols = "<column name='[Calculation_123]' caption='Line Total' datatype='real' role='measure'><calculation class='tableau' formula='[amt] * 2'/></column>"
    r, res, root = _roundtrip(BASE, cols)
    c = _col(root, "Calculation_123")
    assert c is not None and c.get("caption") == "Line Total"
    # second pass is stable
    r2 = convert_datasource(ET.fromstring(res.xml))
    assert write_tds(r2.document, CONN).xml == res.xml


def test_metric_metadata_and_name_restored():
    cols = ("<column name='[Calculation_9]' caption='Total Amount' datatype='real' role='measure' hidden='true' default-format='n#,##0'>"
            "<calculation class='tableau' formula='SUM([amt])'/></column>")
    r, _, root = _roundtrip(BASE, cols)
    (m,) = r.document.metrics
    assert m.datatype is not None
    c = _col(root, "Calculation_9")
    assert c is not None and c.get("caption") == "Total Amount" and c.get("hidden") == "true" and c.get("default-format") == "n#,##0"


@pytest.mark.parametrize("t", ["spatial", "tuple", "table"])
def test_opaque_datatype_restored(t):
    r, _, root = _roundtrip(rel() + recs("orders", [("g", t)]))
    rec = next(x for x in root.iter("metadata-record") if x.findtext("remote-name") == "g")
    assert rec.findtext("local-type") == t
    assert any(l.kind == "unsupported_datatype" and l.stashed for l in r.losses)


def test_stale_witness_falls_back_and_records_loss():
    cols = "<column name='[Calculation_123]' caption='Line Total' datatype='real' role='measure'><calculation class='tableau' formula='[amt] * 2'/></column>"
    r = convert_datasource(ds(BASE, cols))
    f = next(f for f in r.document.datasets[0].fields if f.custom_extensions and "Calculation_123" in f.custom_extensions[0].data)
    doc = r.document.model_copy(deep=True)
    g = next(g for g in doc.datasets[0].fields if g.name == f.name)
    object.__setattr__(g, "label", "Edited by hand")
    res = write_tds(doc, CONN)
    assert "stash_stale" in [x.kind for x in res.losses]
    assert "Calculation_123" not in res.xml
    assert re.search(r'caption="Edited by hand"|caption="' + re.escape(f.name) + '"', res.xml)


def test_composite_key_relationship_written_as_and():
    from .test_units import join_ds
    inner = ("<expression op='AND'><expression op='='><expression op='[a].[id]'/><expression op='[b].[a_id]'/></expression>"
             "<expression op='='><expression op='[a].[k2]'/><expression op='[b].[k2]'/></expression></expression>")
    r = convert_datasource(join_ds(f"<clause type='join'>{inner}</clause>"))
    res = write_tds(r.document, CONN)
    assert "relationship" not in {x.kind for x in res.losses}
    root = ET.fromstring(res.xml)
    (e,) = [x for x in root.iter("relationship")]
    top = e.find("expression")
    assert top.get("op") == "AND" and len(top.findall("expression")) == 2
    r2 = convert_datasource(ET.fromstring(res.xml))
    (rl,) = r2.document.relationships
    assert len(rl.from_columns) == 2 and rl.from_columns == r.document.relationships[0].from_columns


def _sf_ds():
    return ET.fromstring(
        "<datasource name='s'><connection class='snowflake' server='a.snowflakecomputing.com' dbname='MYDB' schema='S' warehouse='WH' service='R'>"
        "<relation name='orders' table='[S].[ORDERS]' type='table'/>" + recs("orders", [("AMT", "real")]) + "</connection></datasource>")


def test_two_part_source_qualified_and_connection_stored():
    r = convert_datasource(_sf_ds())
    assert r.document.datasets[0].source == "MYDB.S.ORDERS"
    assert "source_qualified" in kinds(r)
    ext = next(x for x in r.document.custom_extensions if x.vendor_name == "TABLEAU")
    import json
    c = json.loads(ext.data)["connection"]
    assert c["dbname"] == "MYDB" and c["server"] == "a.snowflakecomputing.com" and c["warehouse"] == "WH"


def test_to_tds_uses_stored_connection_defaults(tmp_path):
    import yaml
    from ossie_tableau import cli
    r = convert_datasource(_sf_ds())
    y = tmp_path / "m.ossie.yaml"
    y.write_text(yaml.safe_dump(r.document.model_dump(mode="json", exclude_none=True)))
    assert cli.main(["to-tds", str(y), "-o", str(tmp_path / "o")]) == 0
    xml = (tmp_path / "o" / "m.tds").read_text()
    assert "a.snowflakecomputing.com" in xml and "MYDB" in xml


FILTER = ("<filter class='categorical' column='[name]'><groupfilter function='member' level='[name]' member='&quot;EMEA&quot;' "
          "user:ui-domain='database' xmlns:user='http://www.tableausoftware.com/xml/user'/></filter>")
STASH_COLS = ("<column name='[name]' datatype='string' role='dimension'><aliases><alias key='&quot;EMEA&quot;' value='Europe'/></aliases></column>"
              "<column name='[Calculation_7]' caption='Scaled' datatype='real' role='measure'>"
              "<calculation class='tableau' formula='SUM([amt]) * [Parameters].[Parameter 1]'/></column>"
              "<drill-paths><drill-path name='Geo'><field>[name]</field><field>[amt]</field></drill-path>"
              "<drill-path name='Time'><field>[when]</field></drill-path></drill-paths>" + FILTER)


def _stash_source():
    return ds(BASE, STASH_COLS)


def test_stashed_constructs_converted_with_one_loss_each():
    param = ET.fromstring("<column name='[Parameter 1]' caption='Mult' datatype='integer' param-domain-type='range' role='measure' value='2'>"
                          "<calculation class='tableau' formula='2'/></column>")
    r = convert_datasource(_stash_source(), params=[param])
    by = {}
    for x in r.losses:
        by.setdefault(x.kind, []).append(x)
    assert len(by["hierarchy"]) == 2 and all(x.stashed for x in by["hierarchy"])
    assert by["datasource_filter"][0].stashed and by["parameters"][0].stashed and by["aliases"][0].stashed


def test_stashed_constructs_restored_and_stable():
    param = ET.fromstring("<column name='[Parameter 1]' caption='Mult' datatype='integer' param-domain-type='range' role='measure' value='2'>"
                          "<calculation class='tableau' formula='2'/></column>")
    r = convert_datasource(_stash_source(), params=[param])
    res = write_tds(r.document, CONN)
    kinds_ = {x.kind for x in res.losses}
    assert "datasource_filter_restored" in kinds_ and kinds_ <= KNOWN_KINDS
    root = ET.fromstring(res.xml)
    assert [d.get("name") for d in root.iter("drill-path")] == ["Geo", "Time"]
    assert root.find("filter") is not None and root.find("filter").get("class") == "categorical"
    assert _col(root, "Parameter 1") is not None and _col(root, "Parameter 1").get("caption") == "Mult"
    assert [a.get("value") for a in _col(root, "name").iter("alias")] == ["Europe"]
    assert 'xmlns:user' in res.xml and 'ns0' not in res.xml
    # converting the written TDS again reproduces the same stash, and writing it again is byte-stable
    r2 = convert_datasource(ET.fromstring(res.xml))
    assert write_tds(r2.document, CONN).xml == res.xml


def test_stash_not_restored_when_model_changed():
    r = convert_datasource(_stash_source())
    doc = r.document.model_copy(deep=True)
    object.__setattr__(doc.datasets[0], "name", "renamed")
    res = write_tds(doc, CONN)
    assert any(x.kind == "stash_stale" for x in res.losses)
    assert "<drill-path" not in res.xml


def test_initial_sql_restored_on_connection():
    d = ds(BASE).__class__  # noqa: F841 - keep helper import side effects explicit
    x = ET.fromstring("<datasource name='d'><connection class='snowflake' one-time-sql='SET x = 1'>" + BASE + "</connection></datasource>")
    r = convert_datasource(x)
    assert any(l.kind == "initial_sql" and l.stashed for l in r.losses)
    root = ET.fromstring(write_tds(r.document, CONN).xml)
    assert [c.get("one-time-sql") for c in root.iter("connection") if c.get("class") == "snowflake"] == ["SET x = 1"]


def _multi_conn_ds(*sqls):
    nc = "".join(f"<named-connection name='c{i}'><connection class='snowflake' one-time-sql='{q}'/></named-connection>" for i, q in enumerate(sqls))
    return ET.fromstring("<datasource name='d'><connection class='federated'><named-connections>" + nc + "</named-connections>" + BASE + "</connection></datasource>")


def _sf_sql(r):
    root = ET.fromstring(write_tds(r.document, CONN).xml)
    return [c.get("one-time-sql") for c in root.iter("connection") if c.get("class") == "snowflake"]


def test_two_connections_different_sql_merged_and_reported():
    r = convert_datasource(_multi_conn_ds("SET a = 1", "SET b = 2"))
    assert [x.kind for x in r.losses].count("initial_sql") == 1
    res = write_tds(r.document, CONN)
    root = ET.fromstring(res.xml)
    sql = [c.get("one-time-sql") for c in root.iter("connection") if c.get("class") == "snowflake"]
    assert sql == ["SET a = 1\nSET b = 2"]
    assert [x.kind for x in res.losses].count("initial_sql_merged") == 1


def test_two_connections_identical_sql_restored_once_without_merge_loss():
    r = convert_datasource(_multi_conn_ds("SET a = 1", "SET a = 1"))
    assert [x.kind for x in r.losses].count("initial_sql") == 1
    res = write_tds(r.document, CONN)
    root = ET.fromstring(res.xml)
    assert [c.get("one-time-sql") for c in root.iter("connection") if c.get("class") == "snowflake"] == ["SET a = 1"]
    assert "initial_sql_merged" not in [x.kind for x in res.losses]


def test_single_connection_has_no_merge_loss():
    x = ET.fromstring("<datasource name='d'><connection class='snowflake' one-time-sql='SET x = 1'>" + BASE + "</connection></datasource>")
    res = write_tds(convert_datasource(x).document, CONN)
    assert "initial_sql_merged" not in [l.kind for l in res.losses]


# ---- bin / categorical-bin / group rows ----

def _bin_ds(calc_xml: str, params: str = ""):
    from tests.test_units import BASE, ds  # noqa: PLC0415
    return convert_datasource(ds(BASE, calc_xml + params))


def _expr(r, name, dialect="TABLEAU"):
    f = next(f for f in r.document.datasets[0].fields if f.name == name)
    return next(d.expression for d in f.expression.dialects if d.dialect.value == dialect)


def test_bin_with_literal_size_becomes_floor_field():
    r = _bin_ds("<column name='[B]' caption='AmtBin' datatype='integer' role='dimension'>"
                "<calculation class='bin' formula='[amt]' size='50' peg='0'/></column>")
    assert _expr(r, "AmtBin") == "FLOOR([amt] / 50) * 50"
    assert _expr(r, "AmtBin", "ANSI_SQL") == "FLOOR(amt / 50) * 50"
    (loss,) = [x for x in r.losses if x.kind == "bin"]
    assert loss.category == "approximated"


def test_bin_size_from_parameter_uses_current_value_and_stashes_parameter():
    p = ("<column name='[Parameter 2]' caption='Size' datatype='integer' param-domain-type='range' role='measure' value='200'>"
         "<calculation class='tableau' formula='200'/></column>")
    r = _bin_ds("<column name='[B]' caption='AmtBin' datatype='integer' role='dimension'>"
                "<calculation class='bin' formula='[amt]' size-parameter='[Parameters].[Parameter 2]' peg='0'/></column>", p)
    assert _expr(r, "AmtBin") == "FLOOR([amt] / 200) * 200"
    assert any(x.kind == "bin" and "parameter" in x.detail for x in r.losses)
    assert any(x.kind == "parameters" and x.stashed for x in r.losses)


def test_bin_with_peg_or_unknown_size_stays_a_loss():
    r = _bin_ds("<column name='[B]' datatype='integer' role='dimension'><calculation class='bin' formula='[amt]' size='5' peg='3'/></column>")
    assert [x.kind for x in r.losses if x.kind == "bin"] and not any(f.name == "B" for f in r.document.datasets[0].fields)


def test_categorical_bin_becomes_case_field():
    r = _bin_ds("<column name='[G]' caption='Grp' datatype='string' role='dimension'>"
                "<calculation class='categorical-bin' column='[name]' new-bin='true'>"
                "<bin value='&quot;A&quot;'><value>&quot;x&quot;</value><value>&quot;y&quot;</value></bin>"
                "<bin value='&quot;B&quot;'><value>&quot;z&quot;</value></bin></calculation></column>")
    assert _expr(r, "Grp") == 'IF [name] IN ("x", "y") THEN "A" ELSEIF [name] IN ("z") THEN "B" ELSE [name] END'
    assert _expr(r, "Grp", "ANSI_SQL") == "CASE WHEN name IN ('x', 'y') THEN 'A' WHEN name IN ('z') THEN 'B' ELSE name END"
    assert any(x.kind == "categorical_bin" and x.category == "approximated" for x in r.losses)


def test_categorical_bin_with_non_literal_member_stays_a_loss():
    r = _bin_ds("<column name='[G]' datatype='string' role='dimension'><calculation class='categorical-bin' column='[name]'>"
                "<bin value='&quot;A&quot;'><value>%null%</value></bin></calculation></column>")
    assert not any(f.name == "G" for f in r.document.datasets[0].fields)
    assert any(x.kind == "categorical_bin" for x in r.losses)


def _numrec_ds(unique: bool):
    obj = lambda i, n: (f"<object id='{i}'><properties><relation connection='c' name='{n}' table='[s].[{n}]' type='table'>"  # noqa: E731
                        "<columns><column name='id' datatype='integer' ordinal='0'/></columns></relation></properties></object>")
    return ET.fromstring(
        "<datasource name='d'><connection class='federated'>"
        "<relation connection='c' name='a' table='[s].[a]' type='table'/><relation connection='c' name='b' table='[s].[b]' type='table'/>"
        + recs("a", [("id", "integer")]) + recs("b", [("id", "integer")]) +
        "</connection><object-graph><objects>" + obj("A", "a") + obj("B", "b") + "</objects><relationships><relationship>"
        "<expression op='='><expression op='[id]'/><expression op='[id]'/></expression>"
        "<first-end-point object-id='A'" + (" unique-key='true'" if unique else "") + "/><second-end-point object-id='B'/>"
        "</relationship></relationships></object-graph>"
        "<column name='[Number of Records]' datatype='integer' role='measure' user:auto-column='numrec' xmlns:user='u'>"
        "<calculation class='tableau' formula='1'/></column></datasource>")


def test_record_count_emits_count_star_when_key_is_known():
    r = convert_datasource(_numrec_ds(unique=True))
    (m,) = r.document.metrics
    assert m.expression.dialects[0].expression == "COUNT(*)"
    assert any(d.expression == "COUNT(*)" for d in m.expression.dialects if d.dialect.value != "TABLEAU")
    (loss,) = [x for x in r.losses if x.kind == "record_count"]
    assert loss.severity == "info"
    assert not any(f.name.endswith("_ONE") for d in r.document.datasets for f in d.fields)


def test_record_count_emits_count_star_without_a_key():
    r = convert_datasource(_numrec_ds(unique=False))
    (m,) = r.document.metrics
    assert m.expression.dialects[0].expression == "COUNT(*)"
    assert "record_count" in {x.kind for x in r.losses}
    assert not any(f.name.endswith("_ONE") for d in r.document.datasets for f in d.fields)


def test_union_member_sources_are_database_qualified():
    x = ET.fromstring(
        "<datasource name='d'><connection class='federated'><named-connections><named-connection name='c'>"
        "<connection class='snowflake' dbname='DB' schema='S'/></named-connection></named-connections>"
        "<relation type='union' all='true' name='U'>"
        "<relation connection='c' name='a' table='[S].[a]' type='table'><columns><column name='x' datatype='string' ordinal='0'/></columns></relation>"
        "<relation connection='c' name='b' table='[S].[b]' type='table'><columns><column name='x' datatype='string' ordinal='0'/></columns></relation>"
        "</relation></connection></datasource>")
    r = convert_datasource(x)
    assert r.document.datasets[0].source == "SELECT * FROM DB.S.a UNION ALL SELECT * FROM DB.S.b"


def test_bin_calculation_restored_byte_stable():
    cols = ("<column name='[B]' caption='AmtBin' datatype='integer' role='dimension'>"
            "<calculation class='bin' formula='[amt]' size='50' peg='0'/></column>")
    r, res, root = _roundtrip(BASE, cols)
    calc = _col(root, "B").find("calculation")
    assert calc.get("class") == "bin" and calc.get("size") == "50"
    r2 = convert_datasource(ET.fromstring(res.xml))
    assert write_tds(r2.document, CONN).xml == res.xml


def test_categorical_bin_calculation_restored():
    cols = ("<column name='[G]' caption='Grp' datatype='string' role='dimension'>"
            "<calculation class='categorical-bin' column='[name]' new-bin='true'>"
            "<bin value='&quot;A&quot;'><value>&quot;x&quot;</value></bin></calculation></column>")
    _, _, root = _roundtrip(BASE, cols)
    assert _col(root, "G").find("calculation").get("class") == "categorical-bin"


def test_aggregate_over_expression_uses_helper_field():
    from .test_units import calc
    r = convert_datasource(ds(BASE, calc("SUM([amt] * 2)", caption="Dbl")))
    (m,) = r.document.metrics
    tab = m.expression.dialects[0].expression
    assert "SUM" in tab
    assert "aggregate_over_expression" not in {x.kind for x in r.losses}


def test_mixed_aggregate_stays_a_loss():
    from .test_units import calc
    r = convert_datasource(ds(BASE, calc("SUM([amt]) / COUNT([amt]) + SUM([amt] * 2)", caption="Mix")))
    assert r.document.metrics and "aggregate_over_expression" not in {x.kind for x in r.losses}


def test_metric_referencing_metric_keeps_a_bare_name():
    from .test_units import calc
    body = calc("SUM([amt])", name="Calculation_1", caption="Base") + calc("SUM([amt]) / [Calculation_1]", name="Calculation_2", caption="Twice")
    r = convert_datasource(ds(BASE, body))
    exprs = {m.name: m.expression.dialects[0].expression for m in r.document.metrics}
    assert exprs.get("Twice") == "SUM([orders].[amt]) / Base"
    assert not any(x.kind == "metric_inlined" for x in r.losses)
    sqls = {m.name: next((d.expression for d in m.expression.dialects if d.dialect.value != "TABLEAU"), None)
            for m in r.document.metrics}
    assert sqls.get("Twice") and "Base" in sqls["Twice"] and "SUM" in sqls["Twice"]


def test_composed_ratio_of_aggregated_calcs_is_metric():
    """Superstore-shaped AOV: ratio of two calc hashes, neither wrap is itself an aggregate."""
    from .test_units import calc
    body = (
        calc("SUM([amt])", name="Calculation_1", caption="Total")
        + calc("COUNT([amt])", name="Calculation_2", caption="N")
        + calc("[Calculation_1] / IF [Calculation_2] = 0 THEN 1 ELSE [Calculation_2] END",
               name="Calculation_3", caption="AOV")
    )
    r = convert_datasource(ds(BASE, body))
    assert "AOV" in {m.name for m in r.document.metrics}
    assert not any(x.kind == "row_level_calc" and x.name == "AOV" for x in r.losses)


def test_countd_of_if_is_metric():
    from .test_units import calc
    r = convert_datasource(ds(BASE, calc("COUNTD(IF [amt] > 1 THEN [name] END)", datatype="integer", caption="Uniq")))
    (m,) = r.document.metrics
    tab = m.expression.dialects[0].expression
    assert "COUNTD" in tab
    assert "aggregate_over_expression" not in {x.kind for x in r.losses}


def test_countd_iif_and_ratio_are_metrics():
    """Superstore RETURNED_ORDER_COUNT + RETURN_RATE."""
    from .test_units import calc
    body = rel() + recs("orders", [("WAS_RETURNED", "boolean"), ("ORDER_ID", "string"), ("amt", "real")])
    cols = (
        calc("COUNTD(IIF([WAS_RETURNED], [ORDER_ID], NULL))", name="Calculation_1", caption="RETURNED_ORDER_COUNT")
        + calc("COUNT([ORDER_ID])", name="Calculation_2", caption="N")
        + calc("([Calculation_1] / IF [Calculation_2] = 0 THEN NULL ELSE [Calculation_2] END) * 100",
               name="Calculation_3", caption="RETURN_RATE")
    )
    r = convert_datasource(ds(body, cols))
    names = {m.name for m in r.document.metrics}
    assert "RETURNED_ORDER_COUNT" in names and "RETURN_RATE" in names
    assert "row_level_calc" not in kinds(r)
    assert "aggregate_over_expression" not in kinds(r)


# ---- _helper_aggregate / _inside_aggregate scanning ----

class _T:
    def __init__(self, name):
        self.name = name


def test_helper_aggregate_handles_paren_inside_double_quoted_string():
    from ossie_tableau.convert import _helper_aggregate
    t = _T("T")
    out = _helper_aggregate('SUM(IF [T].[A] = ")" THEN 1 ELSE 0 END)', [t])
    assert out == ("SUM", 'IF [T].[A] = ")" THEN 1 ELSE 0 END', t)


def test_helper_aggregate_handles_bracket_inside_string_and_escaped_name():
    from ossie_tableau.convert import _helper_aggregate
    t = _T("T")
    assert _helper_aggregate("SUM(IF [T].[A] = '[x' THEN 1 END)", [t]) == ("SUM", "IF [T].[A] = '[x' THEN 1 END", t)
    assert _helper_aggregate("SUM([T].[a]]b] * 2)", [t]) == ("SUM", "[T].[a]]b] * 2", t)


@pytest.mark.parametrize("bad", ["SUM([x", "SUM('abc)", "SUM([x)", "SUM(", "SUM([T].[a]", "SUM([T].[a]) + 1"])
def test_helper_aggregate_malformed_returns_none(bad):
    from ossie_tableau.convert import _helper_aggregate
    assert _helper_aggregate(bad, [_T("T")]) is None


def test_helper_aggregate_never_raises():
    from hypothesis import given, settings, strategies as st
    from ossie_tableau.convert import _helper_aggregate

    @settings(max_examples=300, deadline=None)
    @given(st.text(alphabet="SUMsum()[]'\"\\ ./*T.a1+", max_size=40))
    def run(s):
        _helper_aggregate(s, [_T("T")])
        _helper_aggregate("SUM(" + s, [_T("T")])
    run()


def test_malformed_calc_does_not_fail_the_datasource():
    cols = ("<column name='[Calculation_1]' caption='Bad' datatype='real' role='measure'>"
            "<calculation class='tableau' formula='SUM([amt]'/></column>"
            "<column name='[Calculation_2]' caption='Good' datatype='real' role='measure'>"
            "<calculation class='tableau' formula='SUM([amt]) * 2'/></column>")
    r = convert_datasource(ds(BASE, cols))
    assert r.document is not None and r.stats["metrics"] >= 1


def test_inside_aggregate_checks_every_occurrence():
    from ossie_tableau.convert import _inside_aggregate
    assert _inside_aggregate("[Margin] + SUM([Margin])", "Margin")
    assert not _inside_aggregate("[Margin] + [Margin] * 2", "Margin")
    assert _inside_aggregate("IF [a] = \")\" THEN 0 END + SUM([Margin])", "Margin")
    assert _inside_aggregate("SUM([a]]b])", "a]b")
    assert not _inside_aggregate("SUM('[Margin]') + [Margin]", "Margin")


def test_metric_referenced_inside_aggregate_after_plain_use_is_not_inlined():
    from .test_units import calc
    body = calc("SUM([amt])", name="Calculation_1", caption="Base") + calc("[Calculation_1] + SUM([Calculation_1])", name="Calculation_2", caption="Mix")
    r = convert_datasource(ds(BASE, body))
    assert not any(x.kind == "metric_inlined" for x in r.losses)
    assert "Mix" not in {m.name for m in r.document.metrics}
    assert "calc_reference" in {x.kind for x in r.losses}


def test_caption_becomes_synonym_when_it_differs_from_the_name():
    from .test_units import _fields
    col = "<column name='[amt]' caption='Amount' datatype='real' role='measure'/>"
    f = _fields(convert_datasource(ds(BASE, col)))["amt"]
    assert f.label == "Amount"
    assert f.ai_context is not None and f.ai_context.synonyms == ("Amount",)
    assert f.description is None


def test_caption_equal_to_name_has_no_ai_context():
    from .test_units import _fields
    col = "<column name='[amt]' caption='amt' datatype='real' role='measure'/>"
    f = _fields(convert_datasource(ds(BASE, col)))["amt"]
    assert f.ai_context is None


def test_remote_name_and_caption_are_both_synonyms_when_they_differ_from_the_name():
    from .test_units import _fields
    col = "<column name='[odd col]' caption='Amount' datatype='real' role='measure'/>"
    f = _fields(convert_datasource(ds(BASE, col)))["odd_col"]
    assert f.label == "Amount"
    assert f.ai_context is not None and f.ai_context.synonyms == ("Amount", "odd col")


def test_remote_name_alone_is_a_synonym_when_it_differs_from_the_name():
    from .test_units import _fields
    f = _fields(convert_datasource(ds(BASE)))["odd_col"]
    assert f.ai_context is not None and f.ai_context.synonyms == ("odd col",)


def test_member_aliases_are_not_field_synonyms():
    from .test_units import _fields
    col = ("<column name='[name]' caption='Region' datatype='string' role='dimension'>"
           "<aliases><alias key='&quot;EMEA&quot;' value='Europe'/></aliases></column>")
    r = convert_datasource(ds(BASE, col))
    f = _fields(r)["name"]
    assert f.ai_context is not None and f.ai_context.synonyms == ("Region",)
    assert "Europe" not in f.ai_context.synonyms
    assert "aliases" in {x.kind for x in r.losses}


def test_dataset_source_table_is_a_synonym_when_it_differs_from_the_name():
    body = rel(name="sales", table="[public].[orders]") + recs("sales", [("amt", "real")])
    ds_ = convert_datasource(ds(body)).document.datasets[0]
    assert ds_.name == "sales"
    assert ds_.ai_context is not None and ds_.ai_context.synonyms == ("orders",)


def test_ai_context_round_trips_instructions_and_examples():
    from ossie.models import OssieDocument
    doc = OssieDocument.model_validate({
        "name": "m",
        "ai_context": {"instructions": "prefer sales", "examples": ["show revenue"]},
        "datasets": [{"name": "ORDERS", "source": "DB.SCH.ORDERS", "fields": [
            {"name": "AMT", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "AMT"}]},
             "datatype": "Decimal", "ai_context": {"instructions": "order amount", "examples": ["sum of AMT"]}},
        ]}],
    })
    xml = write_tds(doc, CONN).xml
    back = convert_datasource(ET.fromstring(xml)).document
    assert back.ai_context.instructions == "prefer sales"
    assert back.ai_context.examples == ("show revenue",)
    (f,) = back.datasets[0].fields
    assert f.ai_context.instructions == "order amount"
    assert f.ai_context.examples == ("sum of AMT",)


def test_second_unique_key_hint_stays_on_unique_keys():
    obj = lambda i, n, cols: (
        f"<object id='{i}'><properties><relation connection='c' name='{n}' table='[s].[{n}]' type='table'>"
        "<columns>" + "".join(f"<column name='{c}' datatype='integer' ordinal='0'/>" for c in cols)
        + "</columns></relation></properties></object>")
    x = ET.fromstring(
        "<datasource name='d'><connection class='federated'>"
        "<relation connection='c' name='dim' table='[s].[dim]' type='table'/>"
        "<relation connection='c' name='f1' table='[s].[f1]' type='table'/>"
        "<relation connection='c' name='f2' table='[s].[f2]' type='table'/>"
        + recs("dim", [("id", "integer"), ("code", "integer")])
        + recs("f1", [("id", "integer")]) + recs("f2", [("code", "integer")])
        + "</connection><object-graph><objects>"
        + obj("D", "dim", ["id", "code"]) + obj("F1", "f1", ["id"]) + obj("F2", "f2", ["code"])
        + "</objects><relationships>"
        "<relationship><expression op='='><expression op='[id]'/><expression op='[id]'/></expression>"
        "<first-end-point object-id='D' unique-key='true'/><second-end-point object-id='F1'/></relationship>"
        "<relationship><expression op='='><expression op='[code]'/><expression op='[code]'/></expression>"
        "<first-end-point object-id='D' unique-key='true'/><second-end-point object-id='F2'/></relationship>"
        "</relationships></object-graph></datasource>")
    r = convert_datasource(x)
    dim = next(d for d in r.document.datasets if d.name == "dim")
    assert dim.primary_key == ["id"]
    assert dim.unique_keys == [["code"]]


def _two_table_ds(formula, caption="Ratio"):
    from .test_units import calc
    obj = lambda i, n, col: (
        f"<object id='{i}'><properties><relation connection='c' name='{n}' table='[s].[{n}]' type='table'>"
        f"<columns><column name='{col}' datatype='real' ordinal='0'/></columns></relation></properties></object>")
    return ET.fromstring(
        "<datasource name='d'><connection class='federated'>"
        "<relation connection='c' name='a' table='[s].[a]' type='table'/>"
        "<relation connection='c' name='b' table='[s].[b]' type='table'/>"
        + recs("a", [("x", "real")]) + recs("b", [("y", "real")])
        + "</connection><object-graph><objects>"
        + obj("A", "a", "x") + obj("B", "b", "y")
        + "</objects><relationships><relationship>"
        "<expression op='='><expression op='[x]'/><expression op='[y]'/></expression>"
        "<first-end-point object-id='A'/><second-end-point object-id='B'/>"
        "</relationship></relationships></object-graph>"
        + calc(formula, caption=caption) + "</datasource>")


def test_cross_dataset_sum_ratio_has_warehouse_sql():
    r = convert_datasource(_two_table_ds("SUM([a].[x]) / SUM([b].[y])"))
    (m,) = r.document.metrics
    tab = m.expression.dialects[0].expression
    assert tab == "SUM([a].[x]) / SUM([b].[y])"
    sql = next(d.expression for d in m.expression.dialects if d.dialect.value != "TABLEAU")
    assert "SUM" in sql and "x" in sql and "y" in sql
    assert "sql_untranslated" not in {x.kind for x in r.losses}


def test_cross_dataset_untranslated_keeps_tableau_only():
    r = convert_datasource(_two_table_ds("SUM([a].[x]) / MYFUNC(SUM([b].[y]))"))
    (m,) = r.document.metrics
    assert m.expression.dialects[0].expression.startswith("SUM([a].[x])")
    assert not any(d.dialect.value in ("SNOWFLAKE", "ANSI_SQL", "BIGQUERY") for d in m.expression.dialects)
    assert "sql_untranslated" in {x.kind for x in r.losses}


def test_unknown_datasource_child_survives_round_trip():
    x = ET.fromstring(
        "<datasource name='d'><connection class='snowflake'>"
        + rel() + recs("orders", [("amt", "real")])
        + "</connection><weird-extension foo='1'><inner/></weird-extension></datasource>")
    r = convert_datasource(x)
    assert "unmodeled_xml" in {l.kind for l in r.losses}
    xml = write_tds(r.document, CONN).xml
    back = convert_datasource(ET.fromstring(xml))
    root = ET.fromstring(write_tds(back.document, CONN).xml)
    assert any(c.tag.rsplit("}", 1)[-1] == "weird-extension" for c in root)


def test_snowflake_field_extension_survives_to_tds():
    from ossie.models import OssieDocument
    doc = OssieDocument.model_validate({
        "name": "m",
        "datasets": [{"name": "ORDERS", "source": "DB.SCH.ORDERS", "fields": [
            {"name": "AMT", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "AMT"}]},
             "datatype": "Decimal",
             "custom_extensions": [{"vendor_name": "SNOWFLAKE", "data": "{\"role\":\"measure\"}"}]},
        ]}],
    })
    xml = write_tds(doc, CONN).xml
    back = convert_datasource(ET.fromstring(xml)).document
    (f,) = back.datasets[0].fields
    vendors = {x.vendor_name: x.data for x in (f.custom_extensions or [])}
    assert vendors.get("SNOWFLAKE") == "{\"role\":\"measure\"}"
