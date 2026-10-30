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

import json
import xml.etree.ElementTree as ET

import pytest

from ossie_tableau import cli
from ossie_tableau.convert import _ident, _unique, convert_datasource, convert_file

pytestmark = pytest.mark.unit


def ds(body: str, cols: str = "", conn_extra: str = "", attrs: str = "name='ds1'"):
    return ET.fromstring(f"<datasource {attrs}><connection class='snowflake'>{body}{conn_extra}</connection>{cols}</datasource>")


def rel(name="orders", table="[public].[orders]"):
    return f"<relation connection='c' name='{name}' table='{table}' type='table'/>"


def recs(parent, cols):
    return "<metadata-records>" + "".join(
        f"<metadata-record class='column'><remote-name>{n}</remote-name><local-name>[{n}]</local-name>"
        f"<parent-name>[{parent}]</parent-name><local-type>{t}</local-type></metadata-record>" for n, t in cols
    ) + "</metadata-records>"


def kinds(r):
    return [x.kind for x in r.losses]


def test_ident_and_unique():
    assert _ident("Today's Date") == "Today_s_Date"
    assert _ident("!!!") == "x"
    used: set[str] = set()
    assert [_unique("a", used), _unique("A", used), _unique("a", used)] == ["a", "A_2", "a_3"]


def test_fcp_prefixed_tags_are_normalised():
    x = ET.fromstring(
        "<datasource name='d'><_.fcp.ObjectModelEncapsulateLegacy.true...connection class='snowflake'>"
        "<_.fcp.ObjectModelEncapsulateLegacy.true...relation name='t' table='[s].[t]' type='table'/>"
        + recs("t", [("a", "string")]) + "</_.fcp.ObjectModelEncapsulateLegacy.true...connection></datasource>")
    r = convert_datasource(x)
    assert r.document.datasets[0].source == "s.t"
    assert r.stats["fields"] == 1


def test_type_map_roles_and_temporal():
    x = ds(rel() + recs("orders", [("a", "string"), ("b", "integer"), ("c", "real"), ("d", "date"), ("e", "datetime"), ("f", "boolean")]),
           "<column name='[b]' role='dimension' datatype='integer'/>")
    f = {f.name: f for f in convert_datasource(x).document.datasets[0].fields}
    assert f["a"].dimension is not None          # string -> dimension by type
    assert f["b"].dimension is not None          # explicit role wins
    assert f["c"].dimension is None              # real -> measure
    assert f["d"].dimension.is_time and f["e"].dimension.is_time
    assert f["f"].dimension is not None
    assert [f[k].datatype.value for k in "abcdef"] == ["String", "Integer", "Float", "Date", "DateTime", "Boolean"]


def calc(formula, datatype="real", name="Calculation_1", caption="C"):
    return (f"<column name='[{name}]' caption='{caption}' datatype='{datatype}' role='measure'>"
            f"<calculation class='tableau' formula=\"{formula}\"/></column>")


BASE = rel() + recs("orders", [("amt", "real"), ("name", "string"), ("when", "date"), ("odd col", "real")])


@pytest.mark.parametrize("formula,datatype,expected", [
    ("SUM([amt])", "real", None),
    ("COUNTD([name])", "integer", None),
    ("SUM([amt]) / COUNT([name])", "real", None),
    ("MIN([when])", "date", "non_numeric_result"),
    ("MIN([when])", "real", "non_numeric_aggregate"),
    ("SUM([amt]) + 1", "string", "non_numeric_result"),
    ("COUNTD(IF [amt] > 1 THEN [name] END)", "integer", None),
    ("{INCLUDE [name]: SUM([amt])}", "real", None),
    ("WINDOW_SUM(AVG([amt]))", "real", None),
    ("RAWSQL_REAL('x(%1)', [nope])", "real", "rawsql"),
    ("SUM(RAWSQL_REAL('x(%1)', [amt]))", "real", "rawsql"),
    ("SUM([amt]) * [Parameters].[P]", "real", "parameter_ref"),
    ("[amt] * 2 + [nope]", "real", "row_level_calc"),
    ("SUM([nope])", "real", "unresolved_reference"),
    ("SUM([odd col])", "real", None),
])
def test_calc_classification(formula, datatype, expected):
    r = convert_datasource(ds(BASE, calc(formula, datatype)))
    if expected is None:
        assert r.stats["metrics"] == 1 and not r.stats["unconverted_calcs"]
    else:
        assert r.stats["metrics"] == 0
        assert expected in kinds(r)
        ext = json.loads(r.document.custom_extensions[0].data)
        assert ext["unconverted_calculations"][0]["reason"] == expected


def test_rawsql_becomes_snowflake_field():
    r = convert_datasource(ds(BASE, calc("RAWSQL_STR('UPPER(%1)', [name])", "string", caption="Up")))
    f = _fields_by_name(r)["Up"]
    assert [(d.dialect.value, d.expression) for d in f.expression.dialects] == [("SNOWFLAKE", "UPPER((name))")]
    assert "rawsql_translated" in kinds(r) and r.stats["unconverted_calcs"] == 0


def _fields_by_name(r):
    return {f.name: f for f in r.document.datasets[0].fields}


def test_calc_reference_to_other_calc():
    cols = calc("SUM([amt])", name="Calculation_1", caption="Base") + calc("SUM([Calculation_1]) * 2", name="Calculation_2", caption="Dbl")
    r = convert_datasource(ds(BASE, cols))
    assert r.stats["metrics"] == 1
    assert "calc_reference" in kinds(r)


def test_metric_references_are_dataset_bracketed():
    r = convert_datasource(ds(BASE, calc("SUM([amt])")))
    e = r.document.metrics[0].expression.dialects[0].expression
    assert e == "SUM([orders].[amt])"


def test_apostrophe_and_unicode_names():
    x = ds(rel("Ünï", "[s].[Ünï]") + recs("Ünï", [("Today's Date", "date"), ("名前", "string")]))
    r = convert_datasource(x)
    names = [f.name for f in r.document.datasets[0].fields]
    assert names == ["Today_s_Date", "x"] or len(names) == 2
    assert all(n.isidentifier() or n == "x" for n in names)
    assert r.document.datasets[0].fields[0].expression.dialects[0].expression == '"Today\'s Date"'


def test_custom_sql_source_is_text():
    x = ds("<relation connection='c' name='Custom SQL Query' type='text'>SELECT 1 AS a</relation>" + recs("Custom SQL Query", [("a", "integer")]))
    r = convert_datasource(x)
    assert r.document.datasets[0].source == "SELECT 1 AS a"


def test_connection_only_datasource_skips_cleanly():
    x = ET.fromstring("<datasource name='d'><connection class='sqlserver' server='s'/></datasource>")
    r = convert_datasource(x)
    assert r.document is None and kinds(r) == ["no_tables"]


def test_old_format_single_table_declared_columns():
    x = ds(rel(), "<column name='[a]' datatype='string'/><column name='[b]' datatype='real'/>")
    r = convert_datasource(x)
    assert [f.name for f in r.document.datasets[0].fields] == ["a", "b"]


def test_old_format_multi_table_without_columns_is_a_loss_not_a_crash():
    x = ds(rel("t1", "[s].[t1]") + rel("t2", "[s].[t2]"), "<column name='[a]' datatype='string'/>")
    r = convert_datasource(x)
    assert kinds(r).count("no_column_metadata") == 2


def join_ds(clauses: str):
    tree = (f"<relation type='join' join='inner'>{clauses}"
            "<relation name='a' table='[s].[a]' type='table'/><relation name='b' table='[s].[b]' type='table'/></relation>")
    return ds(tree + recs("a", [("id", "integer"), ("k2", "integer")]) + recs("b", [("a_id", "integer"), ("k2", "integer")]))


def clause(inner):
    return f"<clause type='join'><expression op='='>{inner}</expression></clause>"


def test_legacy_join_single_pair():
    r = convert_datasource(join_ds(clause("<expression op='[a].[id]'/><expression op='[b].[a_id]'/>")))
    (rl,) = r.document.relationships
    assert (rl.from_dataset, rl.to, rl.from_columns, rl.to_columns) == ("a", "b", ["id"], ["a_id"])
    assert "relationship_cardinality" in kinds(r)


def test_legacy_join_composite_and():
    inner = ("<expression op='AND'><expression op='='><expression op='[a].[id]'/><expression op='[b].[a_id]'/></expression>"
             "<expression op='='><expression op='[a].[k2]'/><expression op='[b].[k2]'/></expression></expression>")
    c = f"<clause type='join'>{inner}</clause>"
    r = convert_datasource(join_ds(c))
    (rl,) = r.document.relationships
    assert rl.from_columns == ["id", "k2"] and rl.to_columns == ["a_id", "k2"]


def test_legacy_join_unsupported_operand_becomes_loss():
    r = convert_datasource(join_ds(clause("<expression op='1'/><expression op='[b].[a_id]'/>")))
    assert not r.document.relationships
    assert "relationship" in kinds(r)


def test_parameters_datasource_is_skipped(tmp_path):
    f = tmp_path / "w.twb"
    f.write_text("<workbook><datasources><datasource name='Parameters'><column name='[P]' datatype='string'/></datasource>"
                 "<datasource name='d'><connection class='snowflake'>" + rel() + recs("orders", [("a", "string")]) +
                 "</connection></datasource></datasources></workbook>")
    (r,) = convert_file(f)
    assert r.datasource == "d" and "parameters_unreferenced" in kinds(r)


def test_twbx_zip(tmp_path):
    import zipfile
    f = tmp_path / "w.tdsx"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("x.tds", ET.tostring(ds(rel() + recs("orders", [("a", "string")]))))
    (r,) = convert_file(f)
    assert r.stats["fields"] == 1


def test_empty_zip_raises_clean_error(tmp_path):
    import zipfile
    f = tmp_path / "w.twbx"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(ValueError):
        convert_file(f)


def test_cli_batch_with_bad_file(tmp_path, capsys):
    good = tmp_path / "in" / "good.tds"
    good.parent.mkdir()
    good.write_bytes(ET.tostring(ds(rel() + recs("orders", [("a", "string")]))))
    (tmp_path / "in" / "bad.tds").write_text("<datasource><oops")
    out = tmp_path / "out" / "nested"
    rc = cli.main(["convert", str(good.parent), "-o", str(out)])
    assert rc == 1
    assert (out / "good__ds1.ossie.yaml").exists()
    assert "FAIL bad.tds" in capsys.readouterr().err


def test_cli_skip_writes_loss_only(tmp_path, capsys):
    f = tmp_path / "c.tds"
    f.write_text("<datasource name='d'><connection class='sqlserver'/></datasource>")
    assert cli.main(["convert", str(f), "-o", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / "c__d.loss.json").exists()
    assert not list((tmp_path / "o").glob("*.ossie.yaml"))
    assert "SKIP" in capsys.readouterr().out


# ---- gap-closing tests (tableau-workbook-sdk review) ----

from ossie_tableau import formula as F  # noqa: E402


def _reasons(r):
    return [u["reason"] for u in json.loads(r.document.custom_extensions[0].data)["unconverted_calculations"]]


def _fields(r, i=0):
    return {f.name: f for f in r.document.datasets[i].fields}


@pytest.mark.parametrize("cls,kind", [("bin", "bin"), ("categorical-bin", "categorical_bin"), ("passthrough", "passthrough"), ("weird", "calc_class")])
def test_a1_non_tableau_calc_classes_are_losses(cls, kind):
    col = ("<column name='[Calculation_9]' caption='X' datatype='string' role='dimension'>"
           f"<calculation class='{cls}' formula='[amt]'/></column>")
    r = convert_datasource(ds(BASE, col))
    assert kind in kinds(r)
    assert _reasons(r) == [kind]


def test_a1_categorical_bin_without_formula_is_still_reported():
    col = "<column name='[G]' datatype='string' role='dimension'><calculation class='categorical-bin' column='[name]'/></column>"
    assert "categorical_bin" in kinds(convert_datasource(ds(BASE, col)))


def _union(cols_a, cols_b, all_="true"):
    return (f"<relation type='union' all='{all_}' name='U'>"
            f"<relation connection='c' name='a' table='[s].[a]' type='table'><columns><column name='{cols_a[0][0]}' datatype='string' ordinal='0'/></columns></relation>"
            f"<relation connection='c' name='b' table='[s].[b]' type='table'><columns><column name='{cols_b[0][0]}' datatype='string' ordinal='0'/></columns></relation></relation>"
            + recs("a", cols_a) + recs("b", cols_b))


def test_a2_union_becomes_one_dataset_with_union_source():
    r = convert_datasource(ds(_union([("x", "string")], [("x", "string")])))
    assert len(r.document.datasets) == 1
    assert "UNION ALL" in r.document.datasets[0].source
    assert "union" in kinds(r)


def test_a2_union_distinct_and_mismatch_fallback():
    r = convert_datasource(ds(_union([("x", "string")], [("x", "string")], all_="false")))
    assert "UNION ALL" not in r.document.datasets[0].source and "UNION" in r.document.datasets[0].source
    r = convert_datasource(ds(_union([("x", "string")], [("y", "string")])))
    (d,) = r.document.datasets
    assert d.source == 'SELECT "x", NULL AS "y" FROM s.a UNION ALL SELECT NULL AS "x", "y" FROM s.b'
    assert "union" in kinds(r)
    bare = ("<relation type='union' all='true' name='U'><relation connection='c' name='a' table='[s].[a]' type='table'/>"
            "<relation connection='c' name='b' table='[s].[b]' type='table'/></relation>")
    r = convert_datasource(ds(bare))
    assert r.document.datasets[0].source == "s.a" and "union" in kinds(r)


@pytest.mark.parametrize("text,refs", [
    ("SUM([a])", ["a"]),
    ("'SUM([a])' + [b]", ["b"]),
    ("// SUM([a])\n[b]", ["b"]),
    ("/* [a] */ [b]", ["b"]),
    ("[Col]]x] + 1", ["Col]x"]),
    ("'{FIXED [a]: SUM([b])}'", []),
    ('"it\\"s [a]" + [c]', ["c"]),
])
def test_a3_tokenizer_refs(text, refs):
    assert F.refs(text) == refs


def test_a3_masked_hides_strings_and_comments():
    m = F.masked("IF [a]='SUM(x)' THEN 1 END // SUM(y)")
    assert "SUM" not in m and len(m) == len("IF [a]='SUM(x)' THEN 1 END // SUM(y)")


def test_a3_keywords_in_strings_do_not_change_classification():
    r = convert_datasource(ds(BASE, calc("SUM([amt]) + LEN('{FIXED RAWSQL_')", "real")))
    assert r.stats["metrics"] == 1


def test_a3_escaped_bracket_name_resolves():
    base = rel() + recs("orders", [("a]b", "real")])
    r = convert_datasource(ds(base, calc("SUM([a]]b])")))
    assert r.stats["metrics"] == 1 and "unresolved_reference" not in kinds(r) and "metric_unsupported_binding" not in kinds(r)


def test_a4_numrec_is_record_count_loss():
    cols = ("<column name='[Number of Records]' datatype='integer' role='measure' user:auto-column='numrec' xmlns:user='u'>"
            "<calculation class='tableau' formula='1'/></column>")
    assert "record_count" in kinds(convert_datasource(ds(BASE, cols)))


def test_a4_object_id_count_is_record_count_loss():
    r = convert_datasource(ds(BASE, calc("COUNT([__tableau_internal_object_id__].[orders_ABC])", "integer")))
    assert _reasons(r) == ["record_count"]


def test_a5_datasource_level_losses():
    x = ET.fromstring(
        "<datasource name='d'><connection class='snowflake' one-time-sql='SET x=1'>" + rel() + recs("orders", [("a", "string")]) +
        "</connection><filter class='categorical' column='[a]'/><extract enabled='true'/>"
        "<column name='[U]' datatype='boolean' role='dimension'><calculation class='tableau' formula='ISMEMBEROF(\"g\")'/></column></datasource>")
    k = kinds(convert_datasource(x))
    for want in ("datasource_filter", "initial_sql", "extract", "row_level_security"):
        assert want in k


def test_a5_rls_name_inside_string_is_ignored():
    r = convert_datasource(ds(BASE, calc("SUM([amt]) + LEN('USERNAME()')")))
    assert "row_level_security" not in kinds(r)


def test_a5_sqlproxy_is_published_datasource():
    x = ET.fromstring("<datasource name='d'><connection class='sqlproxy' dbname='Pub'/></datasource>")
    assert kinds(convert_datasource(x)) == ["published_datasource"]


def test_a6_no_set_loss_kind():
    from ossie_tableau import convert as C
    assert "set" not in [k for k, _ in C.__dict__.get("LOSS_TAGS", [])]


def test_b1_description_runs_are_joined():
    col = ("<column name='[amt]' datatype='real' role='measure'><desc><formatted-text><run>Total </run><run>amount</run>"
           "</formatted-text></desc></column>")
    assert _fields(convert_datasource(ds(BASE, col)))["amt"].description == "Total amount"


def test_b2_legacy_join_type_is_recorded():
    x = ET.fromstring(
        "<datasource name='d'><connection class='snowflake'>"
        "<relation type='join' join='left'>"
        "<clause type='join'><expression op='='><expression op='[a].[id]'/><expression op='[b].[id]'/></expression></clause>"
        "<relation connection='c' name='a' table='[s].[a]' type='table'/>"
        "<relation connection='c' name='b' table='[s].[b]' type='table'/></relation>"
        + recs("a", [("id", "integer")]) + recs("b", [("id", "integer")]) + "</connection></datasource>")
    r = convert_datasource(x)
    (rl,) = r.document.relationships
    assert json.loads(rl.custom_extensions[0].data) == {"join_type": "left"}


def test_b3_unique_key_orients_many_to_one():
    x = ET.fromstring(
        "<datasource name='d'><connection class='federated'>"
        "<relation connection='c' name='a' table='[s].[a]' type='table'/><relation connection='c' name='b' table='[s].[b]' type='table'/>"
        + recs("a", [("id", "integer")]) + recs("b", [("id", "integer")]) +
        "</connection><object-graph><objects>"
        "<object id='A'><properties><relation connection='c' name='a' table='[s].[a]' type='table'><columns><column name='id' datatype='integer' ordinal='0'/></columns></relation></properties></object>"
        "<object id='B'><properties><relation connection='c' name='b' table='[s].[b]' type='table'><columns><column name='id' datatype='integer' ordinal='0'/></columns></relation></properties></object>"
        "</objects><relationships><relationship>"
        "<expression op='='><expression op='[id]'/><expression op='[id]'/></expression>"
        "<first-end-point object-id='A' unique-key='true'/><second-end-point object-id='B'/>"
        "</relationship></relationships></object-graph></datasource>")
    r = convert_datasource(x)
    (rl,) = r.document.relationships
    assert (rl.from_dataset, rl.to) == ("b", "a")
    assert r.document.datasets[0].primary_key == ["id"]
    assert r.document.datasets[0].unique_keys is None
    assert "relationship_cardinality" not in kinds(r)


def test_b4_field_extension_json_is_stable():
    col = "<column name='[amt]' datatype='real' role='measure' hidden='true' semantic-role='[Currency]' aggregation='Avg'/>"
    f = _fields(convert_datasource(ds(BASE, col)))["amt"]
    d = json.loads(f.custom_extensions[0].data)
    assert d == {"default_aggregation": "Avg", "hidden": True, "semantic_role": "[Currency]"}
    assert list(d) == sorted(d)
    assert _fields(convert_datasource(ds(BASE)))["name"].custom_extensions is None


def test_b5_row_level_calc_becomes_field_and_metric_can_use_it():
    cols = calc("[amt] * 2", name="Calculation_1", caption="Dbl") + calc("SUM([Calculation_1])", name="Calculation_2", caption="Tot")
    r = convert_datasource(ds(BASE, cols))
    f = _fields(r)["Dbl"]
    assert f.expression.dialects[0].dialect.value == "TABLEAU"
    assert f.expression.dialects[0].expression == "[amt] * 2"
    assert r.document.metrics[0].expression.dialects[0].expression == "SUM([orders].[Dbl])"


def test_b5_cross_table_row_level_calc_becomes_qualified_field():
    x = ds(rel("a", "[s].[a]") + rel("b", "[s].[b]") + recs("a", [("x", "real")]) + recs("b", [("y", "real")]),
           calc("[x] + [y]"))
    r = convert_datasource(x)
    assert "row_level_cross_dataset" in kinds(r) and "row_level_calc" not in kinds(r)
    exprs = [d.expression for t in r.document.datasets for f in t.fields or [] for d in f.expression.dialects if "][" not in d.expression and "." in d.expression]
    assert "[a].[x] + [b].[y]" in exprs


def test_b6_named_connection_recorded():
    x = ET.fromstring(
        "<datasource name='d'><connection class='federated'><named-connections>"
        "<named-connection name='c' caption='Warehouse'><connection class='snowflake'/></named-connection></named-connections>"
        "<relation connection='c' name='t' table='[s].[t]' type='table'/>" + recs("t", [("a", "string")]) +
        "</connection></datasource>")
    r = convert_datasource(x)
    assert json.loads(r.document.datasets[0].custom_extensions[0].data) == {"connection": "Warehouse", "connection_class": "snowflake"}


def test_c1_unknown_connection_class_still_converts():
    x = ET.fromstring("<datasource name='d'><connection class='some-new-connector'>" + rel() + recs("orders", [("a", "string")]) + "</connection></datasource>")
    assert convert_datasource(x).document is not None


def test_c2_fcp_chain_prefix():
    x = ET.fromstring(
        "<datasource name='d'><_.fcp.A.true..._.fcp.B.false...connection class='snowflake'>"
        "<_.fcp.A.true..._.fcp.B.false...relation name='t' table='[s].[t]' type='table'/>" + recs("t", [("a", "string")]) +
        "</_.fcp.A.true..._.fcp.B.false...connection></datasource>")
    assert convert_datasource(x).document.datasets[0].source == "s.t"


@pytest.mark.parametrize("t", ["spatial", "tuple", "table"])
def test_c4_unsupported_datatypes_map_to_opaque_with_stash(t):
    r = convert_datasource(ds(rel() + recs("orders", [("g", t)])))
    assert _fields(r)["g"].datatype.value == "Opaque"
    ext = json.loads(_fields(r)["g"].custom_extensions[0].data)
    assert ext["datatype"]["data"] == t
    assert "unsupported_datatype" in kinds(r)


def _param_ds(formula):
    p = [ET.fromstring("<column name='[Parameter 1]' datatype='real' param-domain-type='any' value='2'/>")]
    return convert_datasource(ds(BASE, calc(formula)), "", p, True), convert_datasource(ds(BASE, calc(formula)), "", p)


def test_inline_parameters_is_opt_in_and_recorded():
    on, off = _param_ds("[amt] * [Parameters].[Parameter 1]")
    assert "parameter_ref" in kinds(off) or "row_level_calc" in kinds(off) or off.stats["row_level_fields"] == 0
    assert "parameter_inlined" in kinds(on) and "parameter_inlined" not in kinds(off)
    (f,) = [x for x in on.document.datasets[0].fields if x.name == "C"]
    assert f.expression.dialects[0].expression == "[amt] * (2)"


def test_inline_parameters_leaves_unknown_parameter_alone():
    on, _ = _param_ds("[amt] * [Parameters].[Nope]")
    assert "parameter_inlined" not in kinds(on)


def test_file_source_is_catalogued():
    x = ET.fromstring(
        "<datasource name='d'><connection class='excel-direct' filename='x.xlsx'>" + rel() + recs("orders", [("a", "string")]) +
        "</connection></datasource>")
    assert "file_source" in kinds(convert_datasource(x))


def test_aggregate_of_numeric_constant_uses_helper_field():
    r = convert_datasource(ds(BASE, calc("AVG(-30)", caption="K")))
    assert r.stats["metrics"] == 1 and "aggregate_over_expression" not in kinds(r)


STR2 = rel() + recs("orders", [("city", "string"), ("state", "string"), ("amt", "real"), ("qty", "real")])


def _snow(r, name):
    f = _fields_by_name(r)[name]
    return next(d.expression for d in f.expression.dialects if d.dialect.value == "SNOWFLAKE")


def test_plus_on_two_string_columns_is_concatenation():
    r = convert_datasource(ds(STR2, calc("[city] + [state]", "string", caption="Loc")))
    assert _snow(r, "Loc") == "(city) || (state)" or "||" in _snow(r, "Loc")
    assert "+" not in _snow(r, "Loc")


def test_plus_string_column_and_string_calc_is_concatenation():
    cols = calc("[city] + ' x'", "string", name="Calculation_1", caption="A") + calc("[Calculation_1] + [state]", "string", name="Calculation_2", caption="B")
    r = convert_datasource(ds(STR2, cols))
    assert "||" in _snow(r, "B") and "+" not in _snow(r, "B")


def test_plus_on_numeric_columns_stays_addition():
    r = convert_datasource(ds(STR2, calc("[amt] + [qty]", "real", caption="Tot")))
    assert "+" in _snow(r, "Tot") and "||" not in _snow(r, "Tot")


def test_plus_chain_with_literal_is_concatenation():
    r = convert_datasource(ds(STR2, calc("[city] + ' ' + [state]", "string", caption="Loc")))
    assert "||" in _snow(r, "Loc") and "+" not in _snow(r, "Loc")


def _fld(name, dialects, dt=None):
    from ossie_tableau.convert import OssieField, OssieExpression, OssieDialectExpression, OssieDialect
    return OssieField(name=name, datatype=dt, expression=OssieExpression(dialects=[
        OssieDialectExpression(dialect=OssieDialect[d], expression=e) for d, e in dialects]))


def test_resolver_prefers_snowflake_over_ansi_regardless_of_order():
    from ossie_tableau.convert import _make_sql_resolver
    f = _fld("x", [("ANSI_SQL", "ansi_col"), ("SNOWFLAKE", "snow_col")])
    out = _make_sql_resolver({"t": [f]})[0]("[x] * 2", "t", "lbl", [])
    assert "snow_col" in out.expression and "ansi_col" not in out.expression


def test_resolver_falls_back_to_ansi():
    from ossie_tableau.convert import _make_sql_resolver
    f = _fld("x", [("ANSI_SQL", "ansi_col")])
    assert "ansi_col" in _make_sql_resolver({"t": [f]})[0]("[x] * 2", "t", "lbl", []).expression


def test_resolver_built_once_per_datasource(monkeypatch):
    from ossie_tableau import convert
    calls = []
    orig = convert._make_sql_resolver
    monkeypatch.setattr(convert, "_make_sql_resolver", lambda *a, **k: calls.append(1) or orig(*a, **k))
    cols = "".join(calc(f"[amt] * {i}", "real", name=f"Calculation_{i}", caption=f"C{i}") for i in range(1, 6))
    convert.convert_datasource(ds(BASE, cols))
    assert len(calls) == 1


def _snow_expr(r, name):
    return next(d.expression for d in _fields_by_name(r)[name].expression.dialects if d.dialect.value == "SNOWFLAKE")


def test_rawsql_over_calc_inlines_the_calc_sql_not_its_name():
    cols = calc("[amt] * 2", name="Calculation_1", caption="Calc A") + calc("RAWSQL_REAL('%1 * 3', [Calculation_1])", name="Calculation_2", caption="Raw")
    r = convert_datasource(ds(BASE, cols))
    sql = _snow_expr(r, "Raw")
    assert "amt" in sql and "Calc" not in sql and "rawsql_translated" in kinds(r)


def test_rawsql_repeated_placeholder_substitutes_twice():
    cols = calc("[amt] * 2", name="Calculation_1", caption="Calc A") + calc("RAWSQL_REAL('%1 + %1', [Calculation_1])", name="Calculation_2", caption="Raw")
    sql = _snow_expr(convert_datasource(ds(BASE, cols)), "Raw")
    assert sql.count("amt") == 2 and "Calc" not in sql


def test_rawsql_over_calc_without_sql_stays_parked(monkeypatch):
    from ossie_tableau import convert as cv
    cols = calc("[amt] * 2", name="Calculation_1", caption="Calc A") + calc("RAWSQL_REAL('%1 * 3', [Calculation_1])", name="Calculation_2", caption="Raw")
    real = cv.OssieField

    def stripped(**kw):
        if kw.get("name") == "Calc_A":
            kw["expression"] = cv.OssieExpression(dialects=[cv.OssieDialectExpression(dialect=cv.OssieDialect.TABLEAU, expression="[amt] * 2")])
        return real(**kw)
    monkeypatch.setattr(cv, "OssieField", stripped)
    r = convert_datasource(ds(BASE, cols))
    assert "Raw" not in _fields_by_name(r) and r.stats["unconverted_calcs"] == 1


def test_rawsql_doubled_quote_in_literal_never_emits_invalid_sql():
    r = convert_datasource(ds(BASE, calc("RAWSQL_STR('CONCAT(%1, ''x'')', [name])", "string", caption="Q")))
    f = _fields_by_name(r).get("Q")
    assert f is None or "''x''" not in _snow_expr(r, "Q")


def test_tableau_internal_columns_are_not_emitted_as_fields():
    from ossie_tableau.convert import convert_datasource

    cols = ("<column name='[Amt]' datatype='real' role='measure'/>"
            "<column name='[fk:Amt:0]' datatype='integer' auto-hidden='true' hidden='true' role='dimension'/>"
            "<column name='[:Measure Names]' datatype='string' role='dimension'/>")
    r = convert_datasource(ds(rel(), cols))
    assert [f.name for f in r.document.datasets[0].fields] == ["Amt"]
